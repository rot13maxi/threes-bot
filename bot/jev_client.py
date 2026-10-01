"""Thin stdlib client for the TypeSafe System One API (Jev).

Python 3.9 compatible (no typesafe-sdk — it needs >=3.10).
Reads the API key from (first hit wins): the TYPESAFE_API_KEY env var,
a `.env` file next to this module (git-ignored), or
~/.hermes/credentials/typesafe-api-key.txt.
"""
import json
import os
import time
import urllib.error
import urllib.request

API_URL = "https://api.typesafe.ai/v1/systemone"
KEY_PATH = os.path.expanduser("~/.hermes/credentials/typesafe-api-key.txt")
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_dotenv(path=ENV_PATH):
    """Parse a minimal KEY=VALUE .env into os.environ (existing vars win)."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


class JevClient:
    def __init__(self, key=None, model="jev-latest", timeout=60, max_retries=4):
        _load_dotenv()
        self.key = key or os.environ.get("TYPESAFE_API_KEY")
        if not self.key and os.path.exists(KEY_PATH):
            with open(KEY_PATH) as f:
                self.key = f.read().strip()
        if not self.key:
            raise RuntimeError("No TypeSafe API key found")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        # accounting
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_latency_s = 0.0
        self.model_versions = {}

    def system_one(self, state, questions):
        """state: str | dict; questions: {qid: {type, instructions, ...}}.

        Returns the parsed response dict:
        {"model": ..., "answers": {qid: {...}}, "usage": {...}}
        """
        body = json.dumps({"state": state, "model": self.model, "questions": questions}).encode()
        last_err = None
        for attempt in range(self.max_retries):
            req = urllib.request.Request(
                API_URL,
                data=body,
                method="POST",
                headers={
                    "Authorization": "Bearer " + self.key,
                    "Content-Type": "application/json",
                },
            )
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    out = json.loads(resp.read().decode())
                dt = time.time() - t0
                self.calls += 1
                self.total_latency_s += dt
                usage = out.get("usage", {})
                self.input_tokens += usage.get("input_tokens", 0)
                self.output_tokens += usage.get("output_tokens", 0)
                v = out.get("model")
                if v:
                    self.model_versions[v] = self.model_versions.get(v, 0) + 1
                out["_latency_s"] = round(dt, 3)
                return out
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 429:
                    ra = e.headers.get("retry-after", "")
                    try:
                        wait = float(ra)
                    except ValueError:
                        wait = 2.0 * (attempt + 1)
                    time.sleep(min(wait, 30))
                    continue
                err_body = e.read().decode(errors="replace")[:300]
                raise RuntimeError("HTTP %s: %s" % (e.code, err_body)) from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("Jev call failed after %d attempts: %r" % (self.max_retries, last_err))

    def cost_usd(self):
        return self.input_tokens / 1_000_000 * 0.042  # input $0.042/Mtok, output free

    def summary(self):
        n = max(self.calls, 1)
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd(), 5),
            "avg_latency_s": round(self.total_latency_s / n, 3),
            "model_versions": self.model_versions,
        }


if __name__ == "__main__":
    c = JevClient()
    r = c.system_one(
        {"fact": "the sky is blue", "fact2": "grass is green"},
        {"q1": {"type": "noul", "instructions": "Is the sky described as blue?"},
         "q2": {"type": "choice", "instructions": "Which fact mentions a color of vegetation?",
                 "criteria": {"fact": "mentions sky color", "fact2": "mentions plant color"}}},
    )
    print(json.dumps(r, indent=2)[:800])
    print("summary:", json.dumps(c.summary()))
