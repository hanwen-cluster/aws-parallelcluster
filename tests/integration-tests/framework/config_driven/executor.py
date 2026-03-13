# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/

"""Check executor — runs selected checks in phase order with dependency tracking."""

import logging
import time
from typing import List

from framework.config_driven.models import Check, CheckContext, CheckResult, CheckStatus


class CheckExecutor:
    """Runs a list of checks against a live cluster, collecting results."""

    def run_checks(self, checks: List[Check], context: CheckContext) -> "ExecutionReport":
        """Execute all checks in order, respecting dependencies.

        If a check's dependency has failed or errored, the check is skipped.
        All checks run regardless of prior failures (soft-assertion style)
        unless a dependency is explicitly declared.
        """
        report = ExecutionReport()
        completed = {}  # name -> CheckStatus

        for check in checks:
            # Dependency gate
            skip_reason = self._check_dependencies(check, completed)
            if skip_reason:
                result = CheckResult(
                    name=check.name,
                    status=CheckStatus.SKIP,
                    message=skip_reason,
                )
                report.add(result)
                completed[check.name] = CheckStatus.SKIP
                logging.info("=== SKIP: %s — %s ===", check.name, skip_reason)
                continue

            # Run the check
            logging.info("=== Running check: %s [%s] ===", check.name, check.category)
            start = time.time()
            try:
                check.run(context)
                duration = time.time() - start
                result = CheckResult(
                    name=check.name,
                    status=CheckStatus.PASS,
                    duration_seconds=duration,
                )
                logging.info("=== PASS: %s (%.1fs) ===", check.name, duration)
            except AssertionError as e:
                duration = time.time() - start
                result = CheckResult(
                    name=check.name,
                    status=CheckStatus.FAIL,
                    duration_seconds=duration,
                    message=str(e),
                    error=e,
                )
                logging.error("=== FAIL: %s (%.1fs) — %s ===", check.name, duration, e)
            except Exception as e:
                duration = time.time() - start
                result = CheckResult(
                    name=check.name,
                    status=CheckStatus.ERROR,
                    duration_seconds=duration,
                    message=str(e),
                    error=e,
                )
                logging.error("=== ERROR: %s (%.1fs) — %s ===", check.name, duration, e)

            report.add(result)
            completed[check.name] = result.status

        return report

    @staticmethod
    def _check_dependencies(check: Check, completed: dict) -> str:
        """Return a skip reason if any dependency is not satisfied, else empty string."""
        for dep in check.depends_on:
            status = completed.get(dep)
            if status is None:
                return f"dependency '{dep}' was not executed"
            if status in (CheckStatus.FAIL, CheckStatus.ERROR, CheckStatus.SKIP):
                return f"dependency '{dep}' {status.value}"
        return ""


class ExecutionReport:
    """Collects check results and provides summary reporting."""

    def __init__(self):
        self.results: List[CheckResult] = []

    def add(self, result: CheckResult):
        self.results.append(result)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.FAIL)

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.ERROR)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.SKIP)

    @property
    def total_duration(self) -> float:
        return sum(r.duration_seconds for r in self.results)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and self.errored == 0

    def report(self):
        """Log a structured summary of all check results."""
        logging.info("")
        logging.info("=" * 70)
        logging.info("CONFIG-DRIVEN TEST RESULTS")
        logging.info("=" * 70)
        for r in self.results:
            status_str = f"[{r.status.value:5s}]"
            duration_str = f"({r.duration_seconds:.1f}s)" if r.duration_seconds > 0 else ""
            msg = f"  → {r.message}" if r.message else ""
            logging.info("  %s %-40s %s%s", status_str, r.name, duration_str, msg)
        logging.info("-" * 70)
        logging.info(
            "SUMMARY: %d passed, %d failed, %d errors, %d skipped (%.1fs)",
            self.passed, self.failed, self.errored, self.skipped, self.total_duration,
        )
        logging.info("=" * 70)

    def assert_all_passed(self):
        """Raise AssertionError if any check failed or errored."""
        failures = [r for r in self.results if r.status in (CheckStatus.FAIL, CheckStatus.ERROR)]
        if failures:
            names = ", ".join(r.name for r in failures)
            raise AssertionError(f"{len(failures)} check(s) failed: {names}")
