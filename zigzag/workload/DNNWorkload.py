from zigzag.workload.Workload import Workload
from zigzag.workload.layer_attributes import LayerAttributes
from zigzag.workload.layer_node import LayerNode
from typing import Any


class DNNWorkload(Workload):

    def __init__(self, workload: dict[int, dict[str, Any]], mapping: dict[str, dict[str, Any]], **attr: Any):
        """! Collect all the algorithmic workload information here.
        @return (self): Directed Graph with nodes the layers and edges the connections between layers.
        """
        super().__init__(**attr)

        layer_id_to_obj: dict[int, LayerNode] = {}  # Lookup dict for id to LayerNode object translation
        self.layer_node_list: list[LayerNode] = []

        for layer_id, layer in workload.items():
            # TODO Support other type of layers, such as concatenation, max pooling, BN, etc.
            #  What is special about max pooling?
            # elif type(layer_id) == str and layer_id[0:6] == 'concat':
            #     continue
            layer_name = layer.get("name", f"Layer {layer_id}")
            operator_type = layer.get("operator_type", None)
            if layer_name in mapping.keys():
                for attr_name, attr_va in mapping[layer_name].items():
                    layer[attr_name] = attr_va
            elif operator_type in mapping.keys():
                for attr_name, attr_va in mapping[operator_type].items():
                    layer[attr_name] = attr_va
            else:
                for attr_name, attr_va in mapping["default"].items():
                    layer[attr_name] = attr_va

            # For each item in the dict generate the LayerNode and add it to the dnn graph G
            layer_attributes = LayerAttributes.parse_user_input(layer)
            layer_node = LayerNode(layer_id, layer_attributes, node_name=layer_name)
            # Save this layer_id and LayerNode pair in the layer_id_to_obj dict
            layer_id_to_obj[layer_id] = layer_node
            # self.add_node(layer_id, info=layer_node)
            self.add_workload_node(layer_node)
            self.layer_node_list.append(layer_node)
            # Find all of its operand sources and add edges accordingly
            edges: list[tuple[LayerNode, LayerNode]] = []
            for _, parent_list in layer.get("operand_source", {}).items():
                for parent_id in parent_list:
                    assert parent_id in layer_id_to_obj, f"Illegal reference to non-existent layer with id {parent_id}"
                    parent_layer = layer_id_to_obj[parent_id]
                    edges.append((parent_layer, layer_node))
                    # layer_node.input_operand_source[op] = parent_layer # TODO This feature is not used?
            self.add_workload_edges_from(edges)
