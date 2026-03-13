# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""Unit tests for CheckRegistry and CheckExecutor."""

from unittest.mock import MagicMock

from framework.config_driven.executor import CheckExecutor
from framework.config_driven.models import (
    Check,
    CheckContext,
    CheckStatus,
    ClusterFeatures,
    Phase,
    RuntimeContext,
)
from framework.config_driven.registry import CheckRegistry


class TestCheckRegistry:

    def test_register_and_select(self):
        registry = CheckRegistry()
        check_a = Check(
            name="Always", category="test", phase=Phase.FUNCTIONAL,
            condition=lambda f, r: True, run=lambda ctx: None,
        )
        check_b = Check(
            name="Never", category="test", phase=Phase.FUNCTIONAL,
            condition=lambda f, r: False, run=lambda ctx: None,
        )
        registry.register(check_a)
        registry.register(check_b)

        selected = registry.select_checks(ClusterFeatures(), RuntimeContext())
        assert len(selected) == 1
        assert selected[0].name == "Always"

    def test_select_sorts_by_phase(self):
        registry = CheckRegistry()
        registry.register(Check(
            name="Late", category="t", phase=Phase.TEARDOWN,
            condition=lambda f, r: True, run=lambda ctx: None,
        ))
        registry.register(Check(
            name="Early", category="t", phase=Phase.INFRASTRUCTURE,
            condition=lambda f, r: True, run=lambda ctx: None,
        ))
        selected = registry.select_checks(ClusterFeatures(), RuntimeContext())
        assert [c.name for c in selected] == ["Early", "Late"]


class TestCheckExecutor:

    def _make_context(self):
        return CheckContext(
            cluster=MagicMock(),
            remote_command_executor=MagicMock(),
            scheduler_commands=MagicMock(),
        )

    def test_passing_check(self):
        executor = CheckExecutor()
        checks = [Check(
            name="Pass", category="t", phase=Phase.FUNCTIONAL,
            condition=lambda f, r: True, run=lambda ctx: None,
        )]
        report = executor.run_checks(checks, self._make_context())
        assert report.passed == 1
        assert report.all_passed is True

    def test_failing_check(self):
        def fail(ctx):
            raise AssertionError("expected failure")

        executor = CheckExecutor()
        checks = [Check(
            name="Fail", category="t", phase=Phase.FUNCTIONAL,
            condition=lambda f, r: True, run=fail,
        )]
        report = executor.run_checks(checks, self._make_context())
        assert report.failed == 1
        assert report.all_passed is False

    def test_dependency_skip(self):
        def fail(ctx):
            raise AssertionError("boom")

        executor = CheckExecutor()
        checks = [
            Check(name="First", category="t", phase=Phase.INFRASTRUCTURE,
                  condition=lambda f, r: True, run=fail),
            Check(name="Second", category="t", phase=Phase.FUNCTIONAL,
                  condition=lambda f, r: True, run=lambda ctx: None,
                  depends_on=["First"]),
        ]
        report = executor.run_checks(checks, self._make_context())
        assert report.failed == 1
        assert report.skipped == 1
        assert report.results[1].status == CheckStatus.SKIP
        assert "First" in report.results[1].message

    def test_error_check(self):
        def error(ctx):
            raise RuntimeError("unexpected")

        executor = CheckExecutor()
        checks = [Check(
            name="Error", category="t", phase=Phase.FUNCTIONAL,
            condition=lambda f, r: True, run=error,
        )]
        report = executor.run_checks(checks, self._make_context())
        assert report.errored == 1

    def test_independent_checks_continue_after_failure(self):
        """Checks without dependencies keep running even if a prior check fails."""
        call_order = []

        def fail(ctx):
            call_order.append("fail")
            raise AssertionError("fail")

        def succeed(ctx):
            call_order.append("succeed")

        executor = CheckExecutor()
        checks = [
            Check(name="A-Fail", category="t", phase=Phase.FUNCTIONAL,
                  condition=lambda f, r: True, run=fail),
            Check(name="B-Pass", category="t", phase=Phase.FUNCTIONAL,
                  condition=lambda f, r: True, run=succeed),
        ]
        report = executor.run_checks(checks, self._make_context())
        assert call_order == ["fail", "succeed"]
        assert report.failed == 1
        assert report.passed == 1
