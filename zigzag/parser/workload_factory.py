import re
import logging
from typing import Any

from zigzag.datatypes import LayerDim, LayerOperand, OADimension, UnrollFactor
from zigzag.mapping.spatial_mapping import MappingSingleOADim, SpatialMapping, SpatialMappingHint
from zigzag.parser.WorkloadValidator import WorkloadValidator
from zigzag.parser.accelerator_factory import AcceleratorFactory
from zigzag.utils import UniqueMessageFilter
from zigzag.workload.DNNWorkload import DNNWorkload
from zigzag.workload.layer_attributes import (
    InputOperandSource,
    LayerDimRelation,
    LayerDimSizes,
    LayerEquation,
    LayerOperandPrecision,
    LayerPadding,
    LayerTemporalOrdering,
    MemoryOperandLinks,
)
from zigzag.workload.layer_node import LayerNode

logger = logging.getLogger(__name__)
logger.addFilter(UniqueMessageFilter())


class WorkloadFactory:
    """! Generates a `Workload` instance from the validated and normalized user-provided data."""

    def __init__(self, workload_data: list[dict[str, Any]], mapping_data: list[dict[str, Any]]):
        self.workload_data = workload_data
        self.mapping_data = mapping_data

    def create(self) -> DNNWorkload:
        node_list: list[LayerNode] = []

        for layer_data in self.workload_data:
            layer_node_factory = LayerNodeFactory(layer_data, self.mapping_data)
            layer_node = layer_node_factory.create()
            node_list.append(layer_node)

        return DNNWorkload(node_list)


class LayerNodeFactory:
    """Creates a LayerNode instance from a validated and normalized user definition of a single workload layer"""

    def __init__(self, node_data: dict[str, Any], mapping_data: list[dict[str, Any]]):
        """!
        @node_data validated and normalized user-defined data for a single workload layer
        @mapping_data validated and normalized user-defined data for all mappings
        """
        self.node_data = node_data
        self.mapping_data = mapping_data

    def create(self) -> LayerNode:
        # From node data
        layer_id: int = self.node_data["id"]
        node_name: str = f"Layer{layer_id}"
        layer_type: str = self.node_data["operator_type"]
        equation = self.create_equation()
        layer_dim_sizes = self.create_layer_dim_sizes()
        operand_precision = self.create_operand_precision()
        dimension_relations = self.create_layer_dim_relations()
        constant_operands = self.create_constant_operands()
        input_operand_source = self.create_operand_source()
        padding = self.create_padding()
        pr_layer_dim_sizes = self.create_pr_layer_dim_sizes()

        # From mapping data
        mapping_factory = MappingFactory(layer_type, self.mapping_data)
        spatial_mapping = mapping_factory.create_spatial_mapping()
        spatial_mapping_hint = mapping_factory.create_spatial_mapping_hint()
        core_allocation = mapping_factory.get_core_allocation()
        memory_operand_links = mapping_factory.create_memory_operand_links()
        temporal_ordering = mapping_factory.create_temporal_ordering()

        return LayerNode(
            layer_id=layer_id,
            node_name=node_name,
            layer_type=layer_type,
            equation=equation,
            layer_dim_sizes=layer_dim_sizes,
            operand_precision=operand_precision,
            dimension_relations=dimension_relations,
            constant_operands=constant_operands,
            input_operand_source=input_operand_source,
            spatial_mapping=spatial_mapping,
            spatial_mapping_hint=spatial_mapping_hint,
            core_allocation=core_allocation,
            memory_operand_links=memory_operand_links,
            temporal_ordering=temporal_ordering,
            padding=padding,
            pr_layer_dim_sizes=pr_layer_dim_sizes,
        )

    def create_equation(self) -> LayerEquation:
        equation: str = self.node_data["equation"]
        equation = equation.replace("+=", "=")
        equation = equation.replace("++", "+")
        equation = equation.replace("*", " * ")
        equation = equation.replace("=", " = ")
        equation = equation.replace("+", " + ")
        return LayerEquation(equation)

    def create_layer_dim_sizes(self) -> LayerDimSizes:
        loop_dims = [self.create_layer_dim(x) for x in self.node_data["loop_dims"]]
        loop_sizes: list[UnrollFactor] = self.node_data["loop_sizes"]

        data = {dim: size for dim, size in zip(loop_dims, loop_sizes)}
        return LayerDimSizes(data)

    def create_operand_precision(self) -> LayerOperandPrecision:
        precisions: dict[str, int] = self.node_data["operand_precision"]
        data: dict[LayerOperand, int] = {
            self.create_layer_operand(operand_str): size for operand_str, size in precisions.items()
        }
        return LayerOperandPrecision(data)

    def create_layer_dim_relations(self) -> list[LayerDimRelation]:
        relations: list[LayerDimRelation] = []
        for relation_str in self.node_data["dimension_relations"]:
            match = re.search(WorkloadValidator.LAYER_DIM_RELATION_REGEX, relation_str)
            assert match is not None
            dim_1, coef_2, dim_2, coef_3, dim_3 = match.groups()
            layer_dim_relation = LayerDimRelation(
                dim_1=self.create_layer_dim(dim_1),
                dim_2=self.create_layer_dim(dim_2),
                dim_3=self.create_layer_dim(dim_3),
                coef_2=int(coef_2) if coef_2 is not None else 1,
                coef_3=int(coef_3) if coef_3 is not None else 1,
            )
            relations.append(layer_dim_relation)

        return relations

    def create_constant_operands(self) -> list[LayerOperand]:
        operand_sources: dict[str, int] = self.node_data["operand_source"]
        constant_operands: list[str] = [op for op, source in operand_sources.items() if source == self.node_data["id"]]
        return [self.create_layer_operand(layer_op_str) for layer_op_str in constant_operands]

    def create_operand_source(self) -> InputOperandSource:
        operand_sources: dict[str, int] = self.node_data["operand_source"]
        return {
            self.create_layer_operand(layer_dim_str): source
            for layer_dim_str, source in operand_sources.items()
            if source != self.node_data["id"]
        }

    def create_padding(self) -> LayerPadding:
        if "padding" not in self.node_data:
            return LayerPadding.empty()

        pr_layer_dims: list[LayerDim] = [self.create_layer_dim(x) for x in self.node_data["pr_loop_dims"]]
        # length of the inner list equals 2
        padding_data: list[list[int]] = self.node_data["padding"]
        padding_dict: dict[LayerDim, tuple[int, int]] = {
            layer_dim: (padding_data[i][0], padding_data[i][1]) for i, layer_dim in enumerate(pr_layer_dims)
        }
        return LayerPadding(padding_dict)

    def create_pr_layer_dim_sizes(self) -> LayerDimSizes | None:
        if "pr_loop_sizes" not in self.node_data:
            return None

        pr_layer_dims: list[LayerDim] = [self.create_layer_dim(x) for x in self.node_data["pr_loop_dims"]]
        pr_sizes: list[int] = self.node_data["pr_loop_sizes"]
        size_dict = {layer_dim: size for layer_dim, size in zip(pr_layer_dims, pr_sizes)}
        return LayerDimSizes(size_dict)

    @staticmethod
    def create_layer_dim(name: str) -> LayerDim:
        return LayerDim(name)

    @staticmethod
    def create_layer_operand(name: str) -> LayerOperand:
        return LayerOperand(name)


class MappingFactory:
    def __init__(self, operation_type: str, mapping_data: list[dict[str, Any]]):
        """
        @param operation_type Name of the layer operation for which the Mapping is being constructed.
        @param mapping_data user-given, validated and normalized mapping data for all operation types.
        """
        if operation_type in map(lambda x: x["name"], mapping_data):
            self.mapping_data: dict[str, Any] = next(filter(lambda x: x["name"] == operation_type, mapping_data))
        else:
            self.mapping_data = next(filter(lambda x: x["name"] == "default", mapping_data))
            logger.warning("Operator %s not defined in mapping. Using default mapping instead.", operation_type)

    def get_core_allocation(self) -> int:
        return self.mapping_data["core_allocation"]

    def create_spatial_mapping(self) -> SpatialMapping:
        if self.mapping_data["spatial_mapping"] is None:
            return SpatialMapping.empty()

        user_data: dict[str, list[str]] = self.mapping_data["spatial_mapping"]
        spatial_mapping_dict: dict[OADimension, MappingSingleOADim] = {}

        for oa_dim_str, unrolling_list in user_data.items():
            oa_dim = AcceleratorFactory.create_oa_dim(oa_dim_str)
            mapping_this_oa_dim = self.create_mapping_single_oa_dim(unrolling_list)
            spatial_mapping_dict[oa_dim] = mapping_this_oa_dim

        return SpatialMapping(spatial_mapping_dict)

    def create_mapping_single_oa_dim(self, mapping_data: list[str]) -> MappingSingleOADim:
        mapping_dict: dict[LayerDim, UnrollFactor] = {}

        for single_unrolling in mapping_data:
            layer_dim_str = single_unrolling.split(",")[0]
            unrolling = int(single_unrolling.split(",")[-1])
            layer_dim = LayerNodeFactory.create_layer_dim(layer_dim_str)
            mapping_dict[layer_dim] = unrolling

        return MappingSingleOADim(mapping_dict)

    def create_spatial_mapping_hint(self) -> SpatialMappingHint:
        if "spatial_mapping_hint" not in self.mapping_data or self.mapping_data["spatial_mapping_hint"] is None:
            return SpatialMappingHint.empty()

        user_data: dict[str, list[str]] = self.mapping_data["spatial_mapping_hint"]
        mapping_hint_dict: dict[OADimension, set[LayerDim]] = {
            AcceleratorFactory.create_oa_dim(oa_dim_str): {LayerDim(layer_dim_str) for layer_dim_str in hint_list}
            for oa_dim_str, hint_list in user_data.items()
        }
        return SpatialMappingHint(mapping_hint_dict)

    def create_memory_operand_links(self) -> MemoryOperandLinks:
        user_data: dict[str, str] = self.mapping_data["memory_operand_links"]
        links_dict = {
            LayerNodeFactory.create_layer_operand(layer_op_str): AcceleratorFactory.create_memory_operand(mem_op_str)
            for layer_op_str, mem_op_str in user_data.items()
        }
        return MemoryOperandLinks(links_dict)

    def create_temporal_ordering(self) -> LayerTemporalOrdering:
        """! This attribute lacks support within the MappingValidator. Returns an empty instance in case it is not
        provided (to be compatible with older code) or raises an error if it is present in the user-provided data.
        """
        if "temporal_ordering" not in self.mapping_data or self.mapping_data["temporal_ordering"] is None:
            return LayerTemporalOrdering.empty()

        raise NotImplementedError()