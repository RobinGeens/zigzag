import logging
from typing import Generator


from zigzag.classes.hardware.architecture.memory_instance import MemoryInstance
from zigzag.classes.mapping.spatial.SpatialMapping import SpatialMapping
from zigzag.classes.opt.spatial.generator import UserSpatialMappingGenerator
from zigzag.classes.hardware.architecture.core import Core
from zigzag.classes.hardware.architecture.accelerator import Accelerator
from zigzag.classes.hardware.architecture.memory_hierarchy import MemoryHierarchy
from zigzag.classes.stages.Stage import Stage
from zigzag.classes.stages.SpatialMappingConversionStage import (
    SpatialMappingConversionStage,
)
import copy
from zigzag.classes.workload.layer_node import LayerNode, Relevancy
from zigzag.utils import pickle_deepcopy

logger = logging.getLogger(__name__)


class SpatialMappingGeneratorStage(Stage):
    """! Pipeline stage that finds spatial mappings given a:
    - accelerator
    - core allocation
    - interconnection pattern on the allocated core
    - layer
    The spatial mappings are found using the interconnection pattern present on the core.
    The inner-most memory level served dimensions is used,
    as this is how the memories connect to the operational array."""

    def __init__(
        self,
        list_of_callables,
        *,
        accelerator: Accelerator,
        layer: LayerNode,
        enable_mix_spatial_mapping_generation=False,
        maximize_hardware_utilization=True,
        enable_weight_diagonal_mapping=False,
        **kwargs,
    ):
        """! The class constructor
        Note: list_of_callables does NOT need to include SpatialMappingConversionStage. Although
        this is used, this usage is done automatically."""
        super().__init__(list_of_callables, **kwargs)
        self.accelerator = accelerator
        self.check_layer(layer)
        self.layer = layer
        self.enable_mix_spatial_mapping_generation = enable_mix_spatial_mapping_generation
        self.maximize_hardware_utilization = maximize_hardware_utilization
        self.enable_weight_diagonal_mapping = enable_weight_diagonal_mapping

    @staticmethod
    def check_layer(layer: LayerNode):
        """!Check that the layer includes:
        - the core which it is allocated to
        If not, a ValueError is raised.
        If the layer in main_inputs is not set, False is returned
        @return: True if layer is set correctly
        """
        if layer is None:
            raise ValueError()
        if layer.core_allocation is None:
            logger.critical(f"Layer {layer} has no core allocation.")  # pylint: disable=W1203
            raise ValueError()
        return True

    def run(self) -> Generator:
        """!  Run this stage by generating user-formatted spatial mappings which are converted
        to the memory-level based spatial mapping representation.
        """

        user_provided_spatial_mappings = self.layer.user_spatial_mapping
        core_id = self.layer.core_allocation
        core: Core = self.accelerator.get_core(core_id=core_id)
        oa_dims = core.operational_array.dimensions

        # Mapping fully defined by user, don't generate new ones
        if all(map(lambda x: x in user_provided_spatial_mappings, oa_dims)):
            user_spatial_mappings = [user_provided_spatial_mappings]
        else:
            user_spatial_mapping_generator = UserSpatialMappingGenerator(
                layer=self.layer,
                accelerator=self.accelerator,
                provided_mapping=user_provided_spatial_mappings,
                enable_mix_spatial_mapping_generation=self.enable_mix_spatial_mapping_generation,
            )
            # Get all the USMs by running the generator
            logger.debug("User-provided spatial mappings incomplete. Auto-generating..")
            user_spatial_mappings = [x for x in user_spatial_mapping_generator.run()]

        nb_user_spatial_mappings = len(user_spatial_mappings)

        for i, user_spatial_mapping in enumerate(user_spatial_mappings):
            logger.info(  # pylint: disable=W1203
                f"Launching spatial mapping {i+1}/{nb_user_spatial_mappings}: {user_spatial_mapping}."
            )
            # Set the user_spatial_mapping in the layer, as this is required by SpatialMappingConversionStage
            self.layer.user_spatial_mapping = user_spatial_mapping
            # Note: manual instantiation of spatial mapping conversion stage here. We let that class deal with
            # everything else, including instantion of the actual substages

            # Modify the size of lower input mem to support weight diagonal spatial unrolling (for OX/OY)
            if self.enable_weight_diagonal_mapping:
                (
                    input_mem_size_updated,
                    new_accelerator,
                ) = self.modify_innermost_input_mem_size(core_id, user_spatial_mapping)
            if self.enable_weight_diagonal_mapping and input_mem_size_updated:
                original_accelerator = self.accelerator
                spatial_mapping_conversion_stage = SpatialMappingConversionStage(
                    self.list_of_callables,
                    accelerator=new_accelerator,
                    layer=copy.copy(self.layer),
                    **self.kwargs,
                )
            else:
                spatial_mapping_conversion_stage = SpatialMappingConversionStage(
                    self.list_of_callables,
                    accelerator=self.accelerator,
                    layer=copy.copy(self.layer),
                    **self.kwargs,
                )
            for cme, extra_info in spatial_mapping_conversion_stage.run():
                if self.enable_weight_diagonal_mapping and input_mem_size_updated:
                    # recover back the accelerator if its mem size is adjusted before
                    cme.accelerator = original_accelerator
                yield cme, (user_spatial_mapping, extra_info)

    def modify_innermost_input_mem_size(self, core_id: int, user_spatial_mapping: SpatialMapping):
        # To support OX, OY unrolling, we will scale the lowest input mem size by OXu*OYu
        # to avoid the MemoryTooSmallException in loma stage.
        input_mem_size_updated = False  # flag to indicate if the accelerator is modified.
        core = self.accelerator.get_core(core_id=core_id)
        operational_array = core.operational_array
        oa_dims = operational_array.dimensions
        memory_hierarchy = copy.deepcopy(core.memory_hierarchy)
        innermost_levels = memory_hierarchy.get_inner_memories()
        # get the link from layer op to mem op
        layer_op_to_mem_op: dict = self.layer.memory_operand_links
        # check if it is weight stationary.
        # keep the spatial loop as it was if it is not weight stationary.
        if len(self.layer.constant_operands) > 1:
            return input_mem_size_updated, self.accelerator
        # get weight operand name
        const_operand = self.layer.constant_operands[0]  # weight representation
        # get activation operand name
        act_operand = [operand for operand in self.layer.input_operands if operand != const_operand][0]
        # get name of OX, OY (weight ir layer dims)
        weight_ir_layer_dims: list = self.layer.operand_loop_dim[const_operand][Relevancy.IR]
        # get the oa_dim name served by input innermost memory level
        for memory_level in innermost_levels:
            mem_ops = memory_level.operands
            if layer_op_to_mem_op[act_operand] in mem_ops:
                act_innermost_mem_level = memory_level
                act_served_oa_dim: set = memory_level.served_dimensions
        # check if act is not served in the innermost memories, or it is uti-casting for act.
        # keep the spatial loop as it was if act is not served.
        if "act_served_oa_dim" not in locals() or len(act_served_oa_dim) != 1:
            return input_mem_size_updated, self.accelerator
        else:
            act_served_oa_dim_name = list(act_served_oa_dim)[0].name
        # get the mem scaling factor if OX, OY exist
        mem_scaling_factor = 1
        if act_served_oa_dim_name not in user_spatial_mapping:  # there is no sm loop
            pass
        else:  # there is sm loop on act served oa dim
            act_served_oa_mapping = user_spatial_mapping[act_served_oa_dim_name]
            for layer_dim, layer_size in act_served_oa_mapping.items():
                if layer_dim in weight_ir_layer_dims:
                    mem_scaling_factor *= layer_size

        # scale the mem size
        if mem_scaling_factor == 1:
            # No need to change the input mem size
            return input_mem_size_updated, self.accelerator
        else:
            input_mem_size_updated = True
            # Initialize the new memory hierarchy
            mh_name = memory_hierarchy.name
            new_mh_name = mh_name + "-supporting-diagonal-map"
            new_memory_hierarchy = MemoryHierarchy(operational_array, new_mh_name)
            # Add memories to the new memory hierarchy with the correct attributes
            for curr_mem_level, memory_level in enumerate(memory_hierarchy.mem_level_list):
                memory_instance = memory_level.memory_instance
                if memory_level == act_innermost_mem_level:
                    memory_instance.size *= mem_scaling_factor  # scale here. For others, keep them unchanged.
                operands = tuple(memory_level.operands)
                port_alloc = memory_level.port_alloc_raw
                served_dimensions_vec = memory_level.served_dimensions_vec
                assert len(served_dimensions_vec) >= 1
                served_dimensions = served_dimensions_vec[0]

                new_memory_instance: MemoryInstance = pickle_deepcopy(memory_instance)  # type: ignore
                new_operands: tuple[str] = pickle_deepcopy(operands)  # type: ignore
                new_port_alloc: tuple[dict] = pickle_deepcopy(port_alloc)  # type: ignore
                new_served_dimensions = pickle_deepcopy(served_dimensions)
                new_memory_hierarchy.add_memory(
                    memory_instance=new_memory_instance,
                    operands=new_operands,
                    port_alloc=new_port_alloc,
                    served_dimensions=new_served_dimensions,
                )
            # Create the new core
            id = core.id
            dataflows = core.dataflows
            new_id = id
            new_dataflows = pickle_deepcopy(dataflows)

            new_core = Core(
                id=new_id,
                operational_array=operational_array,
                memory_hierarchy=new_memory_hierarchy,
                dataflows=new_dataflows,  # type: ignore
            )

            # Create the new accelerator
            name = self.accelerator.name
            new_name = name + "-supporting-diagonal-map"
            new_cores = {new_core}
            new_accelerator = Accelerator(
                name=new_name,
                core_set=new_cores,
            )
            return input_mem_size_updated, new_accelerator
