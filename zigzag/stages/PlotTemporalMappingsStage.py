"""
# TODO this file needs to be reworked
"""

import os

from typing import Any


from zigzag.stages.Stage import Stage, StageCallable
from zigzag.cost_model.cost_model import CostModelEvaluation
from zigzag.visualization.results.plot_cme import (
    bar_plot_cost_model_evaluations_breakdown,
)


class PlotTemporalMappingsStage(Stage):
    """! Class that passes through all results yielded by substages, but keeps the TMs cme's and saves a plot."""

    def __init__(self, list_of_callables: list[StageCallable], *, plot_filename_pattern: str, **kwargs: Any):
        """
        @param list_of_callables: see Stage
        @param dump_filename_pattern: filename string formatting pattern, which can use named field whose values will be
        in kwargs (thus supplied by higher level runnables)
        @param kwargs: any kwargs, passed on to substages and can be used in dump_filename_pattern
        """
        super().__init__(list_of_callables, **kwargs)
        self.plot_filename_pattern = plot_filename_pattern

    def run(self):
        """! Run the compare stage by comparing a new cost model output with the current best found result."""
        substage = self.list_of_callables[0](self.list_of_callables[1:], **self.kwargs)
        cmes: list[CostModelEvaluation] = []
        filename = self.plot_filename_pattern
        for cme, extra_info in substage.run():
            assert isinstance(cme, CostModelEvaluation)
            cmes.append(cme)
            yield cme, extra_info
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        bar_plot_cost_model_evaluations_breakdown(cmes, filename)
