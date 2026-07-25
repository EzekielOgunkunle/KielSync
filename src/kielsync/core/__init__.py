"""Framework-independent core of KielSync.

Nothing under ``kielsync.core`` may import Django, read environment
variables, or otherwise depend on the process it is embedded in. The
package is pure Python so that the orchestration logic can be reused,
unit-tested, and specified independently of any web framework.

The boundary is enforced by a test that walks the AST of every module in
this package and fails on any Django import.
"""
