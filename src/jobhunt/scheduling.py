from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import sys

from .config import ROOT, UserError
from .reporting import SHANGHAI
from .config import validated
from .models import Settings
from .timeutil import local_zone


def export_task(ctx):
    # Export only. User imports the task through Windows Task Scheduler.
    if any(c in str(ctx.home) for c in '\r\n"'):
        raise UserError("SCHEDULE_PATH_INVALID")
    path = ctx.home / 'config/settings.yaml'
    cfg = validated(Settings, path) if path.exists() else Settings()
    zone = local_zone(cfg.schedule_timezone)
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)

    def child(parent, name, value=None, **attrs):
        element = ET.SubElement(parent, "{" + ns + "}" + name, attrs)
        if value is not None:
            element.text = value
        return element

    root = ET.Element("{" + ns + "}Task", {"version": "1.2"})
    triggers = child(root, "Triggers")
    trigger = child(triggers, "CalendarTrigger")
    hour, minute = map(int, cfg.nightly_time.split(':'))
    start = (datetime.now(zone)+timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    child(trigger, "StartBoundary", start.isoformat())
    child(trigger, "Enabled", "true")
    child(child(trigger, "ScheduleByDay"), "DaysInterval", "1")
    settings = child(root, "Settings")
    child(settings, "MultipleInstancesPolicy", "IgnoreNew")
    child(settings, "DisallowStartIfOnBatteries", "false")
    child(settings, "StopIfGoingOnBatteries", "false")
    child(settings, "StartWhenAvailable", "true")
    child(settings, "ExecutionTimeLimit", "PT1H")
    actions = child(root, "Actions")
    action = child(actions, "Exec")
    child(action, "Command", sys.executable)
    arguments = f'-X utf8 -m jobhunt --home "{ctx.home}" nightly --dry-run --once-per-day'
    arguments += " --demo --source demo" if ctx.demo else " --source remotive"
    child(action, "Arguments", arguments)
    child(action, "WorkingDirectory", str(ROOT))
    path = ctx.home / "config" / ("nightly.demo.task.xml" if ctx.demo else "nightly.task.xml")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-16", xml_declaration=True)
    return str(path)
