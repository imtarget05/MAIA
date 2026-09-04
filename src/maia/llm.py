"""LLM via Cloudflare Workers AI (§9). MOCK fallback when no creds.

Endpoint: POST https://api.cloudflare.com/client/v4/accounts/{id}/ai/run/{model}
Body: {"messages": [...]}
"""
import requests


class CloudflareLLM:
    def __init__(self, account_id: str = "", api_token: str = "", model: str = "@cf/meta/llama-3.1-8b-instruct"):
        self.account_id = (account_id or "").strip()
        self.api_token = (api_token or "").strip()
        self.model = model.strip() or "@cf/meta/llama-3.1-8b-instruct"

    @property
    def mode(self) -> str:
        return "cloudflare" if (self.account_id and self.api_token) else "mock"

    def chat(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.1) -> str:
        if self.mode == "mock":
            return self._mock(messages)
        url = f"https://api.cloudflare.com/client/v4/accounts/{self.account_id}/ai/run/{self.model}"
        headers = {"Authorization": f"Bearer {self.api_token}", "Content-Type": "application/json"}
        try:
            r = requests.post(url, headers=headers,
                              json={"messages": messages, "max_tokens": max_tokens, "temperature": temperature},
                              timeout=60)
            r.raise_for_status()
            data = r.json()
            # Workers AI returns {"result": {"response": "..."}} for instruct models
            res = data.get("result", {})
            if isinstance(res, dict):
                return res.get("response") or res.get("text") or str(res)
            return str(res)
        except Exception as e:
            return f"[LLM error: {e}] Fallback answer from context only (mock). " + self._mock(messages)

    @staticmethod
    def _mock(messages: list[dict]) -> str:
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        # Extract context tags
        import re

        tags = re.findall(r"\[S\d+\]", user)
        uniq = []
        for t in tags:
            if t not in uniq:
                uniq.append(t)
        cite = " ".join(uniq[:5]) if uniq else "[S1]"
        return (
            f"(MOCK LLM - set CLOUDFLARE creds for real generation) Based on the retrieved context {cite}, "
            f"the answer is summarized from the top-ranked chunks. Please inspect Sources below for evidence.\n\n"
            f"Sources: {', '.join(uniq[:5]) if uniq else 'none'}"
        )
