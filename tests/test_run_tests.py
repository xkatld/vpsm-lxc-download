import contextlib
import io
import unittest
from unittest import mock

from scripts import run_tests


def make_suite(test_method):
    class Scenario(unittest.TestCase):
        runTest = test_method

    return unittest.TestSuite([Scenario()])


class ChineseTestResultTests(unittest.TestCase):
    def run_case(self, test_method):
        stream = io.StringIO()
        stdout = io.StringIO()
        stderr = io.StringIO()
        result = run_tests.ChineseTestResult(stream)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), mock.patch.dict(
            run_tests.os.environ, {"GITHUB_STEP_SUMMARY": ""}
        ):
            result.startTestRun()
            try:
                make_suite(test_method).run(result)
            finally:
                result.stopTestRun()
            result.write_summary(0.125)
        return result, stream.getvalue(), stdout.getvalue(), stderr.getvalue()

    def test_success_buffers_output_and_reports_chinese_summary(self):
        def scenario(case):
            print("正常标准输出")
            print("正常错误输出", file=run_tests.sys.stderr)
            case.assertTrue(True)

        result, report, stdout, stderr = self.run_case(scenario)

        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.success_count, 1)
        self.assertIn("[通过] 用例001：", report)
        self.assertIn("Scenario.runTest", report)
        self.assertIn("[汇总] 测试通过，运行 1 项，耗时 0.125 秒", report)
        self.assertIn("通过 1 项，失败 0 项，错误 0 项", report)
        self.assertNotIn("正常标准输出", report)
        self.assertNotIn("正常错误输出", report)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")

    def test_failure_preserves_traceback_and_buffered_output(self):
        def scenario(case):
            print("失败标准输出")
            print("失败错误输出", file=run_tests.sys.stderr)
            case.fail("模拟断言失败")

        result, report, stdout, stderr = self.run_case(scenario)

        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.failures), 1)
        self.assertIn("[失败] 用例001：", report)
        self.assertIn("[失败详情]", report)
        self.assertIn("Traceback", report)
        self.assertIn("AssertionError: 模拟断言失败", report)
        self.assertIn("失败标准输出", report)
        self.assertIn("失败错误输出", report)
        self.assertIn("失败标准输出", stdout)
        self.assertIn("失败错误输出", stderr)
        self.assertIn("[汇总] 测试未通过", report)
        self.assertIn("失败 1 项", report)
        self.assertIn("子测试分别计入失败和错误数量", report)

    def test_error_preserves_traceback_and_output(self):
        def scenario(case):
            print("错误现场输出")
            raise ValueError("模拟运行错误")

        result, report, stdout, stderr = self.run_case(scenario)

        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 1)
        self.assertIn("[错误] 用例001：", report)
        self.assertIn("[错误详情]", report)
        self.assertIn("Traceback", report)
        self.assertIn("ValueError: 模拟运行错误", report)
        self.assertIn("错误现场输出", report)
        self.assertIn("错误现场输出", stdout)
        self.assertIn("错误 1 项", report)

    def test_skip_records_reason(self):
        @unittest.skip("模拟条件不满足")
        def scenario(case):
            case.fail("跳过用例不应执行")

        result, report, stdout, stderr = self.run_case(scenario)

        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0][1], "模拟条件不满足")
        self.assertIn("[跳过] 用例001：", report)
        self.assertIn("模拟条件不满足", report)
        self.assertIn("跳过 1 项", report)
        self.assertNotIn("跳过用例不应执行", report)

    def test_expected_failure_retains_details(self):
        @unittest.expectedFailure
        def scenario(case):
            print("预期失败现场")
            case.fail("模拟已知缺陷")

        result, report, stdout, stderr = self.run_case(scenario)

        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(result.expectedFailures), 1)
        self.assertIn("[预期失败] 用例001：", report)
        self.assertIn("[预期失败详情]", report)
        self.assertIn("AssertionError: 模拟已知缺陷", report)
        self.assertIn("预期失败现场", report)
        self.assertIn("预期失败 1 项", report)

    def test_unexpected_success_fails_and_preserves_output(self):
        @unittest.expectedFailure
        def scenario(case):
            print("意外成功标准输出")
            print("意外成功错误输出", file=run_tests.sys.stderr)

        result, report, stdout, stderr = self.run_case(scenario)

        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.unexpectedSuccesses), 1)
        self.assertIn("[意外成功] 用例001：", report)
        self.assertIn("意外成功 1 项", report)
        self.assertIn("[汇总] 测试未通过", report)
        self.assertIn("意外成功标准输出", stdout)
        self.assertIn("意外成功错误输出", stderr)

    def test_subtest_failure_uses_parent_number_and_continues(self):
        def scenario(case):
            with case.subTest(index=1):
                print("子测试失败现场")
                case.fail("模拟子测试失败")
            with case.subTest(index=2):
                print("后续子测试已执行")

        result, report, stdout, stderr = self.run_case(scenario)

        self.assertFalse(result.wasSuccessful())
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.success_count, 0)
        self.assertIn("[失败] 用例001：", report)
        self.assertIn("index=1", report)
        self.assertIn("AssertionError: 模拟子测试失败", report)
        self.assertIn("子测试失败现场", report)
        self.assertIn("后续子测试已执行", stdout)
        self.assertNotIn("用例002", report)
        self.assertNotIn("[通过]", report)

    def test_github_summary_uses_chinese_counts(self):
        stream = io.StringIO()
        result = run_tests.ChineseTestResult(stream)
        result.testsRun = 3
        result.success_count = 2
        result.skipped.append((unittest.FunctionTestCase(lambda: None), "不适用"))
        writer = mock.mock_open()
        with mock.patch.dict(run_tests.os.environ, {"GITHUB_STEP_SUMMARY": "测试摘要"}), mock.patch(
            "builtins.open", writer
        ):
            result.write_summary(0.25)
        writer.assert_called_once_with("测试摘要", "a", encoding="utf-8")
        summary = writer().write.call_args.args[0]
        self.assertIn("## 代码测试结果", summary)
        self.assertIn("状态：**通过**，共运行 3 项", summary)
        self.assertIn("| 通过 | 2 |", summary)
        self.assertIn("| 跳过 | 1 |", summary)

    def test_subtest_error_is_recorded_as_error(self):
        def scenario(case):
            with case.subTest(index=1):
                raise RuntimeError("模拟子测试错误")

        result, report, stdout, stderr = self.run_case(scenario)

        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(result.failures), 0)
        self.assertIn("[错误] 用例001：", report)
        self.assertIn("RuntimeError: 模拟子测试错误", report)
        self.assertIn("错误 1 项", report)


class MainTests(unittest.TestCase):
    def test_mocked_discovery_preserves_scope_and_exit_codes(self):
        def success(case):
            print("入口正常输出")

        def failure(case):
            print("入口失败输出")
            case.fail("入口模拟失败")

        def error(case):
            raise RuntimeError("入口模拟错误")

        @unittest.skip("入口模拟跳过")
        def skipped(case):
            case.fail("跳过用例不应执行")

        @unittest.expectedFailure
        def expected_failure(case):
            case.fail("入口预期失败")

        @unittest.expectedFailure
        def unexpected_success(case):
            pass

        def subtest_failure(case):
            with case.subTest(index=1):
                case.fail("入口子测试失败")

        scenarios = (
            (success, 0, "通过"),
            (failure, 1, "失败"),
            (error, 1, "错误"),
            (skipped, 0, "跳过"),
            (expected_failure, 0, "预期失败"),
            (unexpected_success, 1, "意外成功"),
            (subtest_failure, 1, "失败"),
        )
        for test_method, expected_code, status in scenarios:
            with self.subTest(status=status, method=test_method.__name__):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.ExitStack() as stack:
                    discover = stack.enter_context(
                        mock.patch.object(
                            run_tests.unittest.defaultTestLoader,
                            "discover",
                            return_value=make_suite(test_method),
                        )
                    )
                    stack.enter_context(mock.patch.dict(run_tests.os.environ, {"GITHUB_STEP_SUMMARY": ""}))
                    stack.enter_context(mock.patch.object(run_tests.os, "chdir"))
                    stack.enter_context(
                        mock.patch.object(run_tests.sys, "path", list(run_tests.sys.path))
                    )
                    stack.enter_context(contextlib.redirect_stdout(stdout))
                    stack.enter_context(contextlib.redirect_stderr(stderr))
                    exit_code = run_tests.main()

                discover.assert_called_once_with("tests")
                self.assertEqual(exit_code, expected_code)
                self.assertIn(f"[{status}]", stderr.getvalue())
                self.assertIn("运行 1 项", stderr.getvalue())
                if test_method is success:
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertNotIn("入口正常输出", stderr.getvalue())
                if test_method is failure:
                    self.assertIn("入口失败输出", stdout.getvalue())
                    self.assertIn("AssertionError: 入口模拟失败", stderr.getvalue())

    def test_discovery_exception_is_not_suppressed(self):
        with mock.patch.object(run_tests.os, "chdir"), mock.patch.object(
            run_tests.sys, "path", list(run_tests.sys.path)
        ), mock.patch.object(
            run_tests.unittest.defaultTestLoader,
            "discover",
            side_effect=RuntimeError("模拟发现异常"),
        ) as discover:
            with self.assertRaisesRegex(RuntimeError, "模拟发现异常"):
                run_tests.main()

        discover.assert_called_once_with("tests")
