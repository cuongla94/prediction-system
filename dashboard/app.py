from __future__ import annotations

import os

from flask import Flask, render_template

from .db import get_alerts

app = Flask(__name__)


@app.route("/")
def index():
    alerts, demo_reason = get_alerts()
    actionable = [a for a in alerts if a.is_actionable]
    other = [a for a in alerts if not a.is_actionable]
    any_unvalidated = any(not a.calibration_validated for a in alerts)
    return render_template(
        "index.html",
        actionable=actionable,
        other=other,
        demo_reason=demo_reason,
        any_unvalidated=any_unvalidated,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="127.0.0.1", port=port, debug=True)
