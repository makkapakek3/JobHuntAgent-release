from dataclasses import dataclass
from pathlib import Path
import yaml
from pydantic import ValidationError

from .models import Permissions, Preferences, Profile, Settings, digest, redact, redact_data

ROOT = Path(__file__).resolve().parents[2]


class UserError(Exception):
    """Safe error codes; never include arbitrary exceptions or input bodies."""


def yaml_read(path):
    if not path.is_file():
        raise UserError("PROFILE_MISSING: " + str(path) + "；请按 README 创建本地资料或使用 --demo")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, yaml.YAMLError):
        raise UserError("YAML_INVALID: 检查资料格式") from None


def validated(model, path, defaults=None):
    try:
        return model.model_validate({**(defaults or {}), **yaml_read(path)})
    except ValidationError as exc:
        fields = ['.'.join(map(str, e['loc'])) + ':' + e['type'] for e in exc.errors(include_input=False)]
        raise UserError('CONFIG_INVALID: ' + path.name + ' ' + ', '.join(fields)) from None


def read_env(home):
    """No shell evaluation, only API config; environment wins, never print values."""
    import os
    result = {}
    path = home / '.env'
    if path.is_file():
        if path.stat().st_size > 16000:
            raise UserError('ENV_TOO_LARGE')
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            key, separator, value = line.strip().partition('=')
            if separator and key in {'DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL', 'DEEPSEEK_BASE_URL'}:
                result[key] = value.strip().strip('\"\x27')
    for key in {'DEEPSEEK_API_KEY', 'DEEPSEEK_MODEL', 'DEEPSEEK_BASE_URL'}:
        if key in os.environ:
            result[key] = os.environ[key]
    return result


@dataclass
class Context:
    home: Path
    demo: bool = False
    backend_override: str | None = None

    @property
    def db_path(self):
        return self.home / "data" / ("demo.sqlite3" if self.demo else "jobhunt.sqlite3")

    @property
    def reports(self):
        return self.home / "reports" / ("demo" if self.demo else "live")

    def load(self):
        permissions_path = self.home / "config" / "permissions.yaml"
        self.permissions = validated(Permissions, permissions_path) if permissions_path.exists() else Permissions()
        if not self.permissions.read_profile:
            raise UserError("PROFILE_READ_DENIED")
        directory = ROOT / "examples" if self.demo else self.home / "profile"
        self.profile = validated(Profile, directory / "basic_profile.yaml")
        self.preferences = validated(Preferences, directory / "career_preferences.yaml")
        settings_path = self.home / "config" / "settings.yaml"
        defaults = {'matching_backend': 'mock' if self.demo else 'deepseek'}
        self.settings = validated(Settings, settings_path, defaults) if settings_path.exists() else Settings(**defaults)
        if self.backend_override:
            self.settings.matching_backend = self.backend_override
        env = read_env(self.home)
        self.deepseek_model = env.get('DEEPSEEK_MODEL') or self.settings.deepseek_model
        if self.demo:
            summary_path = ROOT / "examples" / "resume_summary.txt"
        else:
            summary_path = (self.home / self.profile.resume_summary_file).resolve()
            allowed_path = (self.home / "profile" / "resumes").resolve()
            if not summary_path.is_relative_to(allowed_path):
                raise UserError("RESUME_PATH_DENIED")
        if not summary_path.is_file():
            raise UserError("RESUME_SUMMARY_MISSING: 请提供本地文本摘要")
        summary_bytes = summary_path.read_bytes()
        if not summary_bytes.strip():
            raise UserError("RESUME_SUMMARY_EMPTY")
        if len(summary_bytes) > 50000:
            raise UserError("RESUME_SUMMARY_TOO_LARGE")
        self.resume_version = digest(summary_bytes.hex())
        # Whitelist: contact, original resume, identity documents never enter matching state.
        self.safe_profile = {"education": self.profile.model_dump()["education"],
                             "skills": self.profile.skills,
                             "projects": [p.model_dump() for p in self.profile.projects],
                             "summary": redact(summary_bytes.decode("utf-8-sig"))}
        self.safe_profile = redact_data(self.safe_profile)
        self.profile_version = digest([self.safe_profile, self.preferences.model_dump(), self.resume_version])
        return self
