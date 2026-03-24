#
# Copyright 2025 University of Southern California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import datetime
import logging
import os
from logging import StreamHandler
from logging.handlers import SysLogHandler, TimedRotatingFileHandler

from pythonjsonlogger import json

from ...context import get_request_user_id_optional

logger = logging.getLogger(__name__)
svc_logger = logging.getLogger("deriva_mcp")


def init_audit_logger(filename="deriva-mcp-audit.log", use_syslog=False):
    """Initialize the structured JSON audit logger.

    Priority order for the log handler:
      1. SysLogHandler to /dev/log (when use_syslog=True and socket is writable)
      2. TimedRotatingFileHandler (daily rotation, no backup count limit)
      3. StreamHandler (last-resort fallback)

    Args:
        filename: Path to the rotating log file (used when syslog is unavailable).
        use_syslog: Prefer syslog over file-based logging.
    """
    log_handler = StreamHandler()  # last-ditch fallback

    syslog_socket = "/dev/log"
    if use_syslog and (os.path.exists(syslog_socket) and os.access(syslog_socket, os.W_OK)):
        try:
            log_handler = SysLogHandler(address=syslog_socket, facility=SysLogHandler.LOG_LOCAL1)
            log_handler.ident = "deriva-mcp-audit: "
            logger.propagate = False
        except Exception as e:
            svc_logger.warning("Failed to initialize syslog audit handler, falling back: %s", e)
            use_syslog = False

    if not use_syslog:
        try:
            log_handler = TimedRotatingFileHandler(
                filename=filename, when="D", interval=1, backupCount=0
            )
        except Exception as e:
            svc_logger.warning(
                "Failed to initialize %s, falling back to StreamHandler: %s",
                log_handler.__class__.__name__,
                e,
            )

    formatter = json.JsonFormatter("{message}", style="{", rename_fields={"message": "event"})
    log_handler.setFormatter(formatter)
    logger.addHandler(log_handler)
    logger.setLevel(logging.INFO)


def audit_event(event, **kwargs):
    """Emit a structured JSON audit event.

    Automatically injects principal from the per-request contextvar when set.
    The field is omitted if the contextvar has no value (pre-auth events where
    no identity is known yet). Callers may provide principal= explicitly to
    override auto-injection with a value computed before the contextvar is set.

    Args:
        event: Event type string (e.g. "entity_insert", "token_verified").
        **kwargs: Additional fields to include in the log entry.
    """
    extra = {}

    if "principal" not in kwargs:
        uid = get_request_user_id_optional()
        if uid is not None:
            extra["principal"] = uid

    log_entry = {
        "event": event,
        "timestamp": datetime.datetime.now().astimezone().isoformat(),
        **extra,
        **kwargs,
    }
    logger.info(log_entry)
