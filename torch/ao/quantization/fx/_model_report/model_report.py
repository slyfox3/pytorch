# mypy: allow-untyped-defs
from collections import OrderedDict
from typing import Any, Callable, Dict, Set, Tuple

import torch
from torch.ao.quantization.fx._equalize import EqualizationQConfig
from torch.ao.quantization.fx._model_report.detector import (
    DETECTOR_IS_POST_OBS_KEY,
    DETECTOR_OBS_ARGS_KEY,
    DETECTOR_OBS_TO_INSERT_KEY,
    DETECTOR_TARGET_NODE_KEY,
    DetectorBase,
    DetectorQConfigInfo,
)
from torch.ao.quantization.fx._model_report.model_report_visualizer import (
    ModelReportVisualizer,
)
from torch.ao.quantization.fx.graph_module import GraphModule
from torch.ao.quantization.observer import ObserverBase
from torch.ao.quantization.qconfig_mapping import QConfig, QConfigMapping


class ModelReport:
    r"""
    The ModelReport class aims to provide users an easy way to diagnose issues that they run into
    with their models. The class works with all traceable GraphModules to help diagnose issues,
    though the requirements on the type of model more-so depends on the specific report the user
    is trying to generate. With respect to the reports, the ModelReport class is initialized with
    a set of Detector classes, each of which generate reports on quantization configuration
    issues a use might have.

    Currently supports generating reports on:
    - Suggestions for per-channel vs. per-tensor quantization (nn.Module)
    - Suggestions for dynamic vs static quantization for linear layers (Graph Modules)
    - Suggestions for input-weight equalization for linear and conv layers (Graph Modules)
    - Suggestions for outlier detection for all layers (Graph Modules)

    The ModelReport class has the primary functionality of inserting observers (primarily the ModelReportObserver)
    where needed for each detector to gather the information it needs, and then after callibration, the ModelReport
    class compiles the report generated by each Detector class into a single report to return to the user. It also
    has the capability to remove all the observers it inserted as well.

    * :attr:`_model` The model we wish to generate the report for. Must be a traceable GraphModule

    * :attr:`_desired_report_detectors` The set of Detectors representing desired reports from the ModelReport class
        Make sure that these are all unique types of detectors [do not have more than 1 of the same class]

    * :attr:`_desired_detector_names` The set of detector names of the _desired_report_detectors.
        This set is generated by calling the get_detector_name() of each detector

    * :attr:`_detector_name_to_observer_fqns` The mapping from each detector to fqns of observers of interest
        The purpose of this is to keep track of what observers were inserted for each detector, so that they
        can be removed at the end if desired

    * :attr:`_prepared_flag` A boolean flag that keeps track of whether we have prepared the model or not
        This is to ensure we only insert observers once with the ModelReport instance

    * :attr:`_removed_observers` A boolean to track if we have removed observers already
        The purpose is to ensure we don't attempt to remove observers twice with the same ModelReport
        instance. This also allows the functionality where we can generate the report multiple times
        as long as we haven't removed the observers yet.

    Note:
        This class was initially designed to work with the Fx Graph Mode workflow in mind. However,
        full functionality is available as long as there is a traceable GraphModule that is being used.
        One method to get a traceable GraphModule without going through the Fx workflow is to use
        the QuantizationTracer class.

    General Flow for Fx workflow:
    1.) Initialize ModelReport object with reports of interest by passing in initialized detector objects and model
    2.) Prepare your model with prepare_fx
    3.) Call model_report.prepare_detailed_calibration to add relevant observers
    4.) Callibrate your model with data
    5.) Call model_report.generate_report on your model to generate report and optionally remove added observers
    Optional
        6.) Call model_report.generate_visualizer to get a ModelReportVisualizer instance
        7.) To help in parsing report information and debugging, view report info as a:
            - Table
            - Histogram
            - Line plot
    8.) Call model_report.generate_qconfigs to generate the qconfigs based on the report suggestions

    Example (with QuantizationTracer):
        >>> # xdoctest: +SKIP
        >>> # get the necessary qconfig
        >>> config = PrepareCustomConfig()
        >>> skipped_module_names, skipped_module_classes = (
        ...     get_skipped_module_name_and_classes(config, False)
        ... )

        >>> # initialize our model and get GraphModule
        >>> model = SomeModel()
        >>> tracer = QuantizationTracer(skipped_module_names, skipped_module_classes)
        >>> graph_module = GraphModule(model, tracer.trace(model))

        >>> # get our set of detectors and ModelReport instance
        >>> detector_set = set(
        ...     [
        ...         DynamicStaticDetector(tolerance=0.5),
        ...         InputWeightEqualizationDetector(ratio_threshold=0.7),
        ...     ]
        ... )
        >>> tracer_reporter = ModelReport(graph_module, tracer_detector_set)

        >>> # now we insert the observers and callibrate the model
        >>> tracer_model_with_observers = tracer_reporter.prepare_detailed_calibration()
        >>> for i in range(num_callibration_batches):
        >>>     example_input = get_callibration_input()
        >>>     tracer_model_with_observers(example_input)

        >>> # finally we generate the reports and optionally remove the observers we inserted
        >>> reports = tracer_reporter.generate_model_report(remove_inserted_observers=True)

        >>> # Optional: we can generate the qconfig mapping based on the suggestions
        >>> qconfigs = model_report.generate_qconfig_mapping()

        >>> # Optional: we can generate the equalization mapping based on the suggestions
        >>> qconfigs = model_report.generate_equalization_mapping()

        >>> # Optional: we get a ModelReportVisualizer instance to do any visualizations desired
        >>> model_report_visualizer = tracer_reporter.generate_visualizer()

    """

    def __init__(self, model: GraphModule, desired_report_detectors: Set[DetectorBase]):
        if len(desired_report_detectors) == 0:
            raise ValueError("Should include at least 1 desired report")

        # keep track of the model we wish to generate report for
        self._model: GraphModule = model

        # keep the reports private so they can't be modified
        self._desired_report_detectors = desired_report_detectors
        self._desired_detector_names = {
            detector.get_detector_name() for detector in desired_report_detectors
        }

        # keep a mapping of desired reports to observers of interest
        # this is to get the readings, and to remove them, can create a large set
        # this set can then be used to traverse the graph and remove added observers
        self._detector_name_to_observer_fqns: Dict[str, Set[str]] = {}

        # initialize each report to have empty set of observers of interest
        for desired_report in self._desired_detector_names:
            self._detector_name_to_observer_fqns[desired_report] = set()

        # flags to ensure that we can only prepare and remove observers once
        self._prepared_flag = False
        self._removed_observers = False

        # store the reports that we generated for visualization purposes
        # initially empty since no reports generated
        self._generated_reports: Dict[str, Dict] = {}

    def get_desired_reports_names(self) -> Set[str]:
        """Returns a copy of the desired reports for viewing"""
        return self._desired_detector_names.copy()

    def get_observers_of_interest(self) -> Dict[str, Set[str]]:
        """Returns a copy of the observers of interest for viewing"""
        return self._detector_name_to_observer_fqns.copy()

    def prepare_detailed_calibration(self) -> GraphModule:
        r"""
        Takes in a graph model and inserts the following observers:
        - ModelReportObserver

        Each observer is inserted based on the desired_reports into the relevant locations

        Right now, each report in self._desired_detector_names has independent insertions
            However, if a module already has a Observer of the same type, the insertion will not occur
            This is because all of the same type of Observer collect same information, so redundant

        Returns the same GraphModule with the observers inserted
        """

        # if already prepared once, cannot prepare again
        if self._prepared_flag:
            raise ValueError(
                "Already ran preparing detailed callibration. Run the report generation next after callibration."
            )

        # loop through each detector, find where placements should be, and keep track
        insert_observers_fqns: Dict[str, Any] = {}

        for detector in self._desired_report_detectors:
            # determine observer points for each detector
            obs_fqn_to_info = detector.determine_observer_insert_points(self._model)
            # map each insert point to the observer to use
            insert_observers_fqns.update(obs_fqn_to_info)
            # update the set of observers this report cares about
            self._detector_name_to_observer_fqns[detector.get_detector_name()] = set(
                obs_fqn_to_info.keys()
            )

        # now insert all the observers at their desired locations
        for observer_fqn in insert_observers_fqns:
            target_node = insert_observers_fqns[observer_fqn][DETECTOR_TARGET_NODE_KEY]
            insert_obs = insert_observers_fqns[observer_fqn][DETECTOR_OBS_TO_INSERT_KEY]
            insert_post = insert_observers_fqns[observer_fqn][DETECTOR_IS_POST_OBS_KEY]
            observer_args = insert_observers_fqns[observer_fqn][DETECTOR_OBS_ARGS_KEY]
            self._insert_observer_around_module(
                observer_fqn, target_node, insert_obs, observer_args, insert_post
            )

        self._prepared_flag = True

        return self._model

    def _insert_observer_around_module(
        self,
        obs_fqn: str,
        target_node: torch.fx.node.Node,
        obs_to_insert: ObserverBase,
        observer_args: Tuple,
        insert_post: bool,
    ):
        r"""
        Helper function that inserts the observer into both the graph structure and the module of the model

        Args
            node_fqn (str): The fully qualified name of the observer we want to insert
            target_node (torch.fx.node.Node): The node in model we are inserting observers around
            obs_to_insert (ObserverBase): The observer we are inserting around target_node
            observer_args (Tuple): The arguments we want to pass into the observer
            insert_post (bool): whether this is meant to be a post observer for this node
        """
        # if we are inserting post, then our target node is the next node
        if insert_post:
            target_node = target_node.next

        with self._model.graph.inserting_before(target_node):
            self._model.add_submodule(obs_fqn, obs_to_insert)
            self._model.graph.create_node(
                op="call_module", target=obs_fqn, args=observer_args
            )

        # recompile model after inserts are made
        self._model.recompile()

    def _get_node_from_fqn(self, node_fqn: str) -> torch.fx.node.Node:
        r"""
        Takes in a node fqn and returns the node based on the fqn

        Args
            node_fqn (str): The fully qualified name of the node we want to find in model

        Returns the Node object of the given node_fqn otherwise returns None
        """
        node_to_return = None
        for node in self._model.graph.nodes:
            # if the target matches the fqn, it's the node we are looking for
            if node.target == node_fqn:
                node_to_return = node
                break

        if node_to_return is None:
            raise ValueError("The node_fqn is was not found within the module.")

        # assert for MyPy
        assert isinstance(node_to_return, torch.fx.node.Node)

        return node_to_return

    def generate_model_report(
        self, remove_inserted_observers: bool
    ) -> Dict[str, Tuple[str, Dict]]:
        r"""
        Generates all the requested reports.

        Note:
            You should have callibrated the model with relevant data before calling this

        The reports generated are specified by the desired_reports specified in desired_reports

        Can optionally remove all the observers inserted by the ModelReport instance

        Args:
            remove_inserted_observers (bool): True to remove the observers inserted by this ModelReport instance

        Returns a mapping of each desired report name to a tuple with:
            The textual summary of that report information
            A dictionary containing relevant statistics or information for that report

        Note:
            Throws exception if we try to generate report on model we already removed observers from
            Throws exception if we try to generate report without preparing for callibration
        """
        # if we haven't prepped model for callibration, then we shouldn't generate report yet
        if not self._prepared_flag:
            raise Exception(  # noqa: TRY002
                "Cannot generate report without preparing model for callibration"
            )

        # if we already removed the observers, we cannot generate report
        if self._removed_observers:
            raise Exception(  # noqa: TRY002
                "Cannot generate report on model you already removed observers from"
            )

        # keep track of all the reports of interest and their outputs
        reports_of_interest = {}

        for detector in self._desired_report_detectors:
            # generate the individual report for the detector
            report_output = detector.generate_detector_report(self._model)
            reports_of_interest[detector.get_detector_name()] = report_output

        # if user wishes to remove inserted observers, go ahead and remove
        if remove_inserted_observers:
            self._removed_observers = True
            # get the set of all Observers inserted by this instance of ModelReport
            all_observers_of_interest: Set[str] = set()
            for desired_report in self._detector_name_to_observer_fqns:
                observers_of_interest = self._detector_name_to_observer_fqns[
                    desired_report
                ]
                all_observers_of_interest.update(observers_of_interest)

            # go through all_observers_of_interest and remove them from the graph and model
            for observer_fqn in all_observers_of_interest:
                # remove the observer from the model
                self._model.delete_submodule(observer_fqn)

                # remove the observer from the graph structure
                node_obj = self._get_node_from_fqn(observer_fqn)

                if node_obj:
                    self._model.graph.erase_node(node_obj)
                else:
                    raise ValueError("Node no longer exists in GraphModule structure")

            # remember to recompile the model
            self._model.recompile()

        # save the generated reports for visualization purposes
        saved_reports: Dict[str, Dict] = {
            report_name: report_tuple[1]
            for report_name, report_tuple in reports_of_interest.items()
        }

        self._generated_reports = saved_reports

        # return the reports of interest
        return reports_of_interest

    def _is_same_info_for_same_key(self, info_dict_a: Dict, info_dict_b: Dict) -> bool:
        r"""
        Takes in two dictionaries and ensures that any common keys between the two have the same
        values.

        Args:
            info_dict_a (Dict): First dictionary we wish to compare
            info_dict_b (Dict): Second dictionary we wish to compare

        Returns True if all shared keys have same values, false otherwise
        """
        # get the set of keys for both
        dict_a_keys: Set = set(info_dict_a.keys())
        dict_b_keys: Set = set(info_dict_b.keys())

        # get the insersection keys and check if same value for both dicts
        intersecting_keys: Set = dict_a_keys.intersection(dict_b_keys)

        for key in intersecting_keys:
            dict_a_val = info_dict_a[key]
            dict_b_val = info_dict_b[key]

            # if it's a tensor we have to handle separately
            if type(dict_a_val) == torch.Tensor:
                # if dict_b_val not tensor, automatically false
                if (
                    type(dict_b_val) != torch.Tensor
                    or sum(dict_a_val != dict_b_val) != 0
                ):
                    return False
            else:
                # for non-tensor vals
                if dict_a_val != dict_b_val:
                    return False

        # if no non matching shared keys found, return true
        return True

    def _reformat_reports_for_visualizer(self) -> OrderedDict:
        r"""
        Takes the generated reports and reformats them into the format that is desired by the
        ModelReportVisualizer

        Returns an OrderedDict mapping module_fqns to their features
        """
        # we want to reorder and reformat the information so it is ordered in terms of order
        # found in the model

        # first create new dict with all modules as keys and features under respective module
        module_fqns_to_features: Dict[str, Dict] = {}

        for report_name in self._generated_reports:
            # get mod -> feature dict and go through
            module_info = self._generated_reports[report_name]

            for module_fqn in module_info:
                # check if already in our accumulation dict
                if module_fqn in module_fqns_to_features:
                    # we merge all the features together
                    new_info: Dict = module_info[module_fqn]
                    present_info: Dict = module_fqns_to_features[module_fqn]

                    # merge them together into the new unioned dict
                    # same features keys -> same info, so okay if override

                    # do safety check to make sure shared keys have same info
                    if self._is_same_info_for_same_key(new_info, present_info):
                        module_fqns_to_features[module_fqn] = {
                            **new_info,
                            **present_info,
                        }
                    else:
                        error_str = "You have the same key with different values across detectors. "
                        error_str += "Someone incorrectly implemented a detector with conflicting keys to existing detectors."
                        raise ValueError(error_str)
                else:
                    # we just set it
                    module_fqns_to_features[module_fqn] = module_info[module_fqn]

        # our ordered dict so that modules can be ordered in order of how they appear in model
        features_by_module: OrderedDict[str, Dict] = OrderedDict()

        # we loop through modules in graph in order
        for fqn, module in self._model.named_modules():
            # find that fqn in fqns_to_features
            if fqn in module_fqns_to_features:
                # add it to our ordered dict
                features_by_module[fqn] = module_fqns_to_features[fqn]

        # return the ordered dict of info we created
        return features_by_module

    def generate_visualizer(self) -> ModelReportVisualizer:
        r"""
        Generates a ModelReportVisualizer instance using the reports generated
        by the generate_model_report() method.

        Returns the generated ModelReportVisualizer instance initialized

        Note:
            Throws exception if attempt to get visualizers without generating report
        """
        # check if user has generated reports at least once
        if len(self._generated_reports) == 0:
            raise Exception(  # noqa: TRY002
                "Unable to generate visualizers without first generating reports"
            )

        # get the ordered dict mapping modules to their full set of collected features / stats
        module_fqns_to_features: OrderedDict = self._reformat_reports_for_visualizer()

        # create and return ModelReportVisualizer instance
        visualizer: ModelReportVisualizer = ModelReportVisualizer(
            module_fqns_to_features
        )

        return visualizer

    def _generate_qconfig_mapping_helper(
        self,
        detector_qconfig_info_combined: Dict[str, DetectorQConfigInfo],
        generation_function: Callable,
    ) -> QConfigMapping:
        r"""
        This helper takes in the compiled detector qconfig info that
        has been compiled together and merges it into a QConfigMapping
        """
        # keep track of the qconfigmapping
        qconfig_mapping = QConfigMapping()

        # loop through each module / fqn and attempt to create QConfigMapping
        for fqn, module in self._model.named_modules():
            # if we have a qconfig info for this module
            if fqn in detector_qconfig_info_combined:
                qconfig_info_compiled = detector_qconfig_info_combined[fqn]

                # now generate the qconfig and add it to the mapping
                generated_qconfig = generation_function(qconfig_info_compiled, module)

                # add to our config
                qconfig_mapping.set_module_name(fqn, generated_qconfig)

        # return compiled mapping
        return qconfig_mapping

    def _update_detector_quantizaiton_qconfig_info(
        self, combined_info: DetectorQConfigInfo, new_info: DetectorQConfigInfo
    ):
        r"""
        Takes in the old and new information and updates the combined information.

        Args:
            combined_info (DetectorQConfigInfo): The DetectorQConfigInfo we are compiling all of the information in
            new_info (DetectorQConfigInfo): The DetectorQConfigInfo with the information we are trying to merge the new info
                into it
        """
        combined_info.is_activation_dynamic = (
            combined_info.is_activation_dynamic or new_info.is_activation_dynamic
        )
        combined_info.is_weight_per_channel = (
            combined_info.is_weight_per_channel or new_info.is_weight_per_channel
        )

    def _update_detector_equalization_qconfig_info(
        self, combined_info: DetectorQConfigInfo, new_info: DetectorQConfigInfo
    ):
        r"""
        Takes in the old and new information and updates the combined information.

        Args:
            combined_info (DetectorQConfigInfo): The DetectorQConfigInfo we are compiling all of the information in
            new_info (DetectorQConfigInfo): The DetectorQConfigInfo with the information we are trying to merge the new info
                into it
        """
        is_equalization_recommended = (
            combined_info.is_equalization_recommended
            or new_info.is_equalization_recommended
        )
        combined_info.is_equalization_recommended = is_equalization_recommended

    def _generate_module_fqn_to_detector_info_mapping(
        self, update_qconfig_info_function: Callable
    ) -> Dict[str, DetectorQConfigInfo]:
        r"""
        Generates a QConfigMapping based on the suggestions of the
        ModelReport API. The generated mapping encompasses all the
        different types of feedback from the different detectors
        all into one place.

        These configs are based on the suggestions provided by the ModelReport API
        and can only be generated once the reports have been generated.

        Args:
            update_qconfig_info_function (Callable) takes in a function that takes in two DetectorQConfigInfo
            and updates the one that is being compiled

        Returns a Dict mapping module_fqns to DetectorQConfigInfo objects

        Note:
            Throws exception if we try to generate mapping on model we already removed observers from
            Throws exception if we try to generate mapping without preparing for callibration
        """
        # if we haven't prepped model for callibration, then we shouldn't generate mapping yet
        if not self._prepared_flag:
            raise Exception(  # noqa: TRY002
                "Cannot generate report without preparing model for callibration"
            )

        # if we already removed the observers, we cannot mapping
        if self._removed_observers:
            raise Exception(  # noqa: TRY002
                "Cannot generate report on model you already removed observers from"
            )

        # keep track of qconfig info for each module across detectors
        detector_qconfig_info_combined: Dict[str, DetectorQConfigInfo] = {}

        for detector in self._desired_report_detectors:
            # get the info from the detector
            detector_info: Dict[str, DetectorQConfigInfo] = detector.get_qconfig_info(
                self._model
            )

            # we go through the modules
            for module_fqn in detector_info:
                # see if we already have info on it
                if module_fqn in detector_qconfig_info_combined:
                    # we combine the current options with what is there
                    current_options = detector_qconfig_info_combined[module_fqn]
                    detector_options = detector_info[module_fqn]

                    update_qconfig_info_function(current_options, detector_options)
                else:
                    # we just use this for now
                    detector_qconfig_info_combined[module_fqn] = detector_info[
                        module_fqn
                    ]

        return detector_qconfig_info_combined

    def generate_qconfig_mapping(self) -> QConfigMapping:
        r"""
        Generates a QConfigMapping based on the suggestions of the
        ModelReport API. The generated mapping encompasses all the
        different types of feedback from the different detectors
        all into one place.

        These configs are based on the suggestions provided by the ModelReport API
        and can only be generated once the reports have been generated.

        Returns a QConfigMapping for the quantization configuration

        Note:
            Throws exception if we try to generate mapping on model we already removed observers from
            Throws exception if we try to generate mapping without preparing for callibration
        """
        # get the mapping info
        detector_qconfig_info_combined = (
            self._generate_module_fqn_to_detector_info_mapping(
                self._update_detector_quantizaiton_qconfig_info
            )
        )

        # we will do a bit of processing and remove fqns that don't have input weight recommended

        # now we generate the QConfig for each of the options
        mapping: QConfigMapping = self._generate_qconfig_mapping_helper(
            detector_qconfig_info_combined, self._quantization_config_generator
        )

        # return the generated mapping
        return mapping

    def _quantization_config_generator(
        self, detector_qconfig_info: DetectorQConfigInfo, module: torch.nn.Module
    ) -> QConfig:
        r"""
        Returns the quantization configuration generated by the DetectorQConfigInfo object
        """
        return detector_qconfig_info.generate_quantization_qconfig(module)

    def _equalization_config_generator(
        self, detector_qconfig_info: DetectorQConfigInfo, module: torch.nn.Module
    ) -> EqualizationQConfig:
        r"""
        We ignore the module argument here, and only focus on thedetector_qconfig_info

        Returns the equalization configuration generated by the DetectorQConfigInfo object
        """
        return detector_qconfig_info.generate_equalization_qconfig()

    def generate_equalization_mapping(self) -> QConfigMapping:
        r"""
        Generates a QConfigMapping based on the suggestions of the
        ModelReport API for equalization. The generated mapping encompasses all the
        different types of feedback from the input-weight equalization detector.

        These configs are based on the suggestions provided by the ModelReport API
        and can only be generated once the reports have been generated.

        Returns a QConfigMapping for the equalization configuration
        """
        # get the mapping info
        detector_qconfig_info_combined = (
            self._generate_module_fqn_to_detector_info_mapping(
                self._update_detector_equalization_qconfig_info
            )
        )

        # now we generate the QConfig for each of the options
        mapping: QConfigMapping = self._generate_qconfig_mapping_helper(
            detector_qconfig_info_combined, self._equalization_config_generator
        )

        # return the generated mapping
        return mapping
