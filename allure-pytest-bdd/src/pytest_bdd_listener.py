import pytest
import doctest
import allure_commons
from allure_commons.utils import now
from allure_commons.utils import uuid4
from allure_commons.model2 import Label
from allure_commons.model2 import Status
from allure_commons.model2 import TestResult

from allure_commons.types import LabelType, AttachmentType
from allure_commons.utils import platform_label
from allure_commons.utils import host_tag, thread_tag
from allure_commons.utils import md5
from .utils import get_uuid
from .utils import get_step_name
from .utils import get_status_details
from .utils import get_pytest_report_status
from allure_commons.model2 import StatusDetails
from functools import partial
from allure_commons.lifecycle import AllureLifecycle
from .utils import get_full_name, get_name, get_params


class PytestBDDListener:
    def __init__(self):
        self.lifecycle = AllureLifecycle()
        self.host = host_tag()
        self.thread = thread_tag()

    def _scenario_finalizer(self, scenario):
        for step in scenario.steps:
            step_uuid = get_uuid(str(id(step)))
            with self.lifecycle.update_step(uuid=step_uuid) as step_result:
                if step_result:
                    step_result.status = Status.SKIPPED
                    self.lifecycle.stop_step(uuid=step_uuid)

    @pytest.hookimpl
    def pytest_bdd_before_scenario(self, request, feature, scenario):
        uuid = get_uuid(request.node.nodeid)
        full_name = get_full_name(feature, scenario)
        name = get_name(request.node, scenario)
        with self.lifecycle.schedule_test_case(uuid=uuid) as test_result:
            test_result.fullName = full_name
            test_result.name = name
            test_result.start = now()
            test_result.historyId = md5(request.node.nodeid)
            test_result.labels.append(Label(name=LabelType.HOST, value=self.host))
            test_result.labels.append(Label(name=LabelType.THREAD, value=self.thread))
            test_result.labels.append(Label(name=LabelType.FRAMEWORK, value="pytest-bdd"))
            test_result.labels.append(Label(name=LabelType.LANGUAGE, value=platform_label()))
            test_result.labels.append(Label(name=LabelType.FEATURE, value=feature.name))
            test_result.parameters = get_params(request.node)

        finalizer = partial(self._scenario_finalizer, scenario)
        request.node.addfinalizer(finalizer)

    @pytest.hookimpl
    def pytest_bdd_after_scenario(self, request, feature, scenario):
        uuid = get_uuid(request.node.nodeid)
        with self.lifecycle.update_test_case(uuid=uuid) as test_result:
            test_result.stop = now()

    @pytest.hookimpl
    def pytest_bdd_before_step(self, request, feature, scenario, step, step_func):
        parent_uuid = get_uuid(request.node.nodeid)
        uuid = get_uuid(str(id(step)))
        with self.lifecycle.start_step(parent_uuid=parent_uuid, uuid=uuid) as step_result:
            step_result.name = get_step_name(step)

    @pytest.hookimpl
    def pytest_bdd_after_step(self, request, feature, scenario, step, step_func, step_func_args):
        uuid = get_uuid(str(id(step)))
        with self.lifecycle.update_step(uuid=uuid) as step_result:
            step_result.status = Status.PASSED
        self.lifecycle.stop_step(uuid=uuid)

    @pytest.hookimpl
    def pytest_bdd_step_error(self, request, feature, scenario, step, step_func, step_func_args, exception):
        uuid = get_uuid(str(id(step)))
        with self.lifecycle.update_step(uuid=uuid) as step_result:
            step_result.status = Status.FAILED
            step_result.statusDetails = get_status_details(exception)
        self.lifecycle.stop_step(uuid=uuid)

    @pytest.hookimpl
    def pytest_bdd_step_func_lookup_error(self, request, feature, scenario, step, exception):
        uuid = get_uuid(str(id(step)))
        with self.lifecycle.update_step(uuid=uuid) as step_result:
            step_result.status = Status.BROKEN
        self.lifecycle.stop_step(uuid=uuid)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = (yield).get_result()
        uuid = get_uuid(report.nodeid)

        status = get_pytest_report_status(report)
        status_details = None

        if call.excinfo:
            message = call.excinfo.exconly()
            if hasattr(report, 'wasxfail'):
                reason = report.wasxfail
                message = (f'XFAIL {reason}' if reason else 'XFAIL') + '\n\n' + message
            trace = report.longreprtext
            status_details = StatusDetails(
                message=message,
                trace=trace)

            exception = call.excinfo.value
            if (status != Status.SKIPPED and _exception_brokes_test(exception)):
                status = Status.BROKEN

        if status == Status.PASSED and hasattr(report, 'wasxfail'):
            reason = report.wasxfail
            message = f'XPASS {reason}' if reason else 'XPASS'
            status_details = StatusDetails(message=message)
        with self.lifecycle.update_test_case(uuid=uuid) as test_result:

            if test_result is None and status is not "passed":
                new_test_result = TestResult()
                new_test_result.uuid = uuid
                new_test_result.name = report.head_line
                new_test_result.status = status
                new_test_result.statusDetails = status_details
                self.lifecycle._items[uuid] = new_test_result
                self.lifecycle.write_test_case(uuid=uuid)

            if test_result and report.when == "setup":
                test_result.status = status
                test_result.statusDetails = status_details

            if report.when == "call" and test_result:
                if test_result.status == Status.PASSED:
                    test_result.status = status
                    test_result.statusDetails = status_details

            if report.when == "teardown" and test_result:
                if status in (Status.FAILED, Status.BROKEN) and test_result.status == Status.PASSED:
                    test_result.status = status
                    test_result.statusDetails = status_details
                if report.caplog:
                    self.attach_data(report.caplog, "log", AttachmentType.TEXT, None)
                if report.capstdout:
                    self.attach_data(report.capstdout, "stdout", AttachmentType.TEXT, None)
                if report.capstderr:
                    self.attach_data(report.capstderr, "stderr", AttachmentType.TEXT, None)

        if report.when == 'teardown':
            self.lifecycle.write_test_case(uuid=uuid)

    @allure_commons.hookimpl
    def attach_data(self, body, name, attachment_type, extension):
        self.lifecycle.attach_data(uuid4(), body, name=name, attachment_type=attachment_type, extension=extension)

    @allure_commons.hookimpl
    def attach_file(self, source, name, attachment_type, extension):
        self.lifecycle.attach_file(uuid4(), source, name=name, attachment_type=attachment_type, extension=extension)


def _exception_brokes_test(exception):
    return not isinstance(exception, (
        AssertionError,
        pytest.fail.Exception,
        doctest.DocTestFailure
    ))
