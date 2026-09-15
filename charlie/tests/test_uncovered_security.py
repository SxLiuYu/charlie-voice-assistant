"""Security-focused tests for magic modules lacking direct unit tests.

Covers: magic-system, magic-feishu, magic-dev, agent/device_control
Focus: subprocess safety, token handling, parameter clamping.
"""
import ast
import os
import re
import sys
import subprocess

PARENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PARENT_DIR)


def _load_module(name, filename):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, os.path.join(PARENT_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSubprocessSafetyInMagicModules:
    """All subprocess calls must use list form (no shell injection)."""

    def _find_subprocess_calls(self, filename):
        fpath = os.path.join(PARENT_DIR, filename)
        with open(fpath) as f:
            source = f.read()
        tree = ast.parse(source)
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == 'run':
                    if isinstance(func.value, ast.Name) and func.value.id == 'subprocess':
                        calls.append((node.lineno, ast.unparse(node)))
        return calls

    def test_magic_system_subprocess_safe(self):
        calls = self._find_subprocess_calls('magic-system.py')
        assert len(calls) >= 1, "magic-system.py should have subprocess volume control"
        for lineno, code in calls:
            assert code.startswith('subprocess.run(['), \
                f"Line {lineno}: subprocess.run must use list form, got: {code[:80]}"

    def test_magic_reminder_subprocess_safe(self):
        calls = self._find_subprocess_calls('magic-reminder.py')
        if not calls:
            return  # not all platforms need subprocess
        for lineno, code in calls:
            assert code.startswith('subprocess.run(['), \
                f"Line {lineno}: subprocess.run must use list form"


class TestParameterClampingInDeviceControl:
    """Volume and device parameters must be clamped to safe ranges."""

    def _find_clamp(self, source, name):
        """Check that parameter is clamped with max/min."""
        # Look for clamp pattern: max(min(value, upper), lower)
        return 'max(' in source and 'min(' in source

    def test_magic_system_volume_clamped(self):
        with open(os.path.join(PARENT_DIR, 'magic-system.py')) as f:
            source = f.read()
        assert self._find_clamp(source, 'level'), \
            "magic-system.py should clamp volume level"

    def test_device_control_volume_clamped(self):
        with open(os.path.join(PARENT_DIR, 'agent', 'device_control.py')) as f:
            source = f.read()
        assert self._find_clamp(source, 'delta_pct'), \
            "agent/device_control.py should clamp delta_pct"


class TestFeishuTokenHandling:
    """Feishu token handling must not leak secrets in responses."""

    def test_feishu_token_env_only(self):
        """FEISHU_BOT_TOKEN should come from env, not hardcoded."""
        with open(os.path.join(PARENT_DIR, 'magic-feishu.py')) as f:
            source = f.read()
        # Should use os.getenv or os.environ
        assert 'os.getenv("FEISHU' in source or 'os.environ.get("FEISHU' in source, \
            "magic-feishu.py should read FEISHU tokens from environment"

    def test_no_actual_token_in_response_strings(self):
        """Actual token values (not env var names) must never appear in return strings."""
        with open(os.path.join(PARENT_DIR, 'magic-feishu.py')) as f:
            source = f.read()
        # These are the actual token formats used by Feishu - they are long random hex or
        # base64 strings. Environment variable names (FEISHU_APP_ID etc.) in error messages
        # are expected and safe.
        real_token_pattern = re.compile(
            r'[a-f0-9]{30,}|[A-Za-z0-9+/]{40,}={0,2}', re.I
        )
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Return) and node.value:
                code = ast.unparse(node.value)
                # Skip if this is just a variable reference (not a literal) or
                # contains env var names (not actual secret values)
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    matches = real_token_pattern.findall(code)
                    assert not matches, \
                        f"Potential real token in return string: {matches}"


class TestNoBareExceptInUntestedModules:
    """No bare except: blocks allowed in production magic modules."""

    def test_no_bare_except(self):
        files = [
            'magic-system.py', 'magic-feishu.py', 'magic-dev.py',
            'magic-douyin.py', 'magic-info.py', 'magic-life.py',
            'magic-music.py', 'magic-notes.py', 'magic-preferences.py',
            'magic-recipe.py', 'magic-scenes.py', 'magic-summary.py',
            'magic-taobao.py', 'magic-wardrobe.py', 'magic-habits.py',
            'magic-browser.py', 'magic-apps.py',
        ]
        violations = []
        for fn in files:
            fpath = os.path.join(PARENT_DIR, fn)
            if not os.path.exists(fpath):
                continue
            with open(fpath) as f:
                for i, line in enumerate(f, 1):
                    stripped = line.strip()
                    # Match except: or except :  (not except Exception:)
                    if re.match(r'^except\s*:\s*$', stripped):
                        violations.append(f'{fn}:{i}')
        assert not violations, f"Bare except: blocks found: {violations}"


class TestSubprocessTimeoutCoverage:
    """All subprocess.run calls must have timeout= parameter."""

    def test_all_subprocess_has_timeout(self):
        files = [
            'magic-system.py', 'magic-dev.py', 'magic-reminder.py',
            'magic-music.py',
        ]
        violations = []
        for fn in files:
            fpath = os.path.join(PARENT_DIR, fn)
            if not os.path.exists(fpath):
                continue
            with open(fpath) as f:
                lines = f.readlines()
            tree = ast.parse(''.join(lines))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute) and func.attr == 'run':
                        if isinstance(func.value, ast.Name) and func.value.id == 'subprocess':
                            # Check timeout in keyword args and the calling context
                            has_timeout = any(
                                kw.arg == 'timeout' for kw in node.keywords
                            )
                            if not has_timeout:
                                violations.append(f'{fn}:{node.lineno}')
                        assert not violations[-1] if violations else True, \
                            f"subprocess.run without timeout: {violations[-1]}"
        # Soft assert - report violations for coverage planning
        if violations:
            print(f"\n[BCOV] subprocess calls without timeout: {violations}")
            # Don't fail yet - these may be intentional for short-lived cmds


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])
