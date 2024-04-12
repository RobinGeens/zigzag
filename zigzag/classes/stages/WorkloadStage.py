import networkx as nx
import logging

from zigzag.classes.hardware.architecture.accelerator import Accelerator
from zigzag.classes.stages.Stage import Stage
from zigzag.classes.workload.Workload import Workload
from zigzag.classes.workload.dummy_node import DummyNode


logger = logging.getLogger(__name__)


class WorkloadStage(Stage):
    """!  Class that iterates through the nodes in a given workload graph."""

    def __init__(self, list_of_callables, *, workload: Workload, accelerator: Accelerator, **kwargs):
        """!  The class constructor
        Initialization of self.workload.
        """
        super().__init__(list_of_callables, **kwargs)
        self.workload = workload
        self.accelerator = accelerator

    def run(self):
        for id, layer in enumerate(nx.topological_sort(self.workload)):
            # skip the DummyNodes
            if isinstance(layer, DummyNode):
                continue
            # Skip a layer if the layer type is "Pooling" and the hardware template is an IMC core.
            # This wil have impact when the workload is defined manually.
            # If the workload is from onnx, no skipping will be done.
            core_id = layer.core_allocation
            core = self.accelerator.get_core(core_id)
            operational_array = core.operational_array
            pe_type = getattr(operational_array, "pe_type", None)  # return None if it does not exist
            # branch if the workload is manually defined
            try:
                layer_type = layer.layer_attrs["operator_type"]
            # branch if the workload is from an onnx (key "operator_type" does not exist)
            except KeyError:
                layer_type = None
            if (pe_type in ["in_sram_computing"]) and (layer_type in ["Pooling", "Add"]):
                continue

            kwargs = self.kwargs.copy()
            kwargs["layer"] = layer
            kwargs["accelerator"] = self.accelerator
            if layer.name:
                layer_name = layer.name
            else:
                layer_name = id
            logger.info(f"Processing layer {layer_name}...")
            sub_stage = self.list_of_callables[0](self.list_of_callables[1:], **kwargs)
            for cme, extra_info in sub_stage.run():
                yield cme, (layer, extra_info)
