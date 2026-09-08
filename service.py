"""
Windows Service wrapper for Job Tracker.

Install:   python service.py install
Start:     python service.py start
Stop:      python service.py stop
Remove:    python service.py remove
Debug:     python service.py debug   (runs in foreground with logging)
"""

import sys
import os

# Ensure the job_tracker directory is on the path before any local imports
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# Load .env early — the Windows service runs without a user session so system
# env vars set via the GUI won't be present. This must happen before config.py
# is imported by any module.
from dotenv import load_dotenv
load_dotenv(os.path.join(BASE_DIR, ".env"))

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
    import threading
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


if HAS_WIN32:
    class JobTrackerService(win32serviceutil.ServiceFramework):
        _svc_name_ = "JobTrackerService"
        _svc_display_name_ = "Job Application Tracker"
        _svc_description_ = (
            "Polls Gmail for job application emails and updates Google Sheets."
        )

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._thread: threading.Thread | None = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)
            if self._thread:
                self._thread.join(timeout=10)

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)

        def _run(self):
            from main import main
            main()


def run_debug():
    """Run the tracker in the foreground for testing (no Windows service needed)."""
    from main import main
    main()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "debug":
        run_debug()
    elif HAS_WIN32:
        win32serviceutil.HandleCommandLine(JobTrackerService)
    else:
        print("pywin32 not installed. Running in debug mode instead.")
        run_debug()
