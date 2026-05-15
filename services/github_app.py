"""GitHub App token minting for private repository indexing."""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from jose import jwt


class GitHubAppError(RuntimeError):
    """Raised when GitHub App credentials or API calls fail."""


@dataclass(frozen=True)
class GitHubInstallationToken:
    token: str
    expires_at: str | None = None


class GitHubAppService:
    def __init__(
        self,
        *,
        app_id: str | None = None,
        private_key: str | None = None,
        api_base_url: str | None = None,
    ) -> None:
        self.app_id = (app_id or os.getenv("GITHUB_APP_ID") or "").strip()
        self.private_key = private_key if private_key is not None else self._private_key_from_env()
        self.api_base_url = (
            api_base_url or os.getenv("GITHUB_API_BASE_URL") or "https://api.github.com"
        ).rstrip("/")

    @staticmethod
    def _private_key_from_env() -> str:
        encoded = os.getenv("GITHUB_APP_PRIVATE_KEY_BASE64")
        if encoded:
            try:
                return base64.b64decode(encoded).decode("utf-8")
            except Exception as exc:
                raise GitHubAppError("GITHUB_APP_PRIVATE_KEY_BASE64 is not valid base64 PEM data.") from exc

        raw = os.getenv("GITHUB_APP_PRIVATE_KEY") or ""
        return raw.replace("\\n", "\n").strip()

    def is_configured(self) -> bool:
        return bool(self.app_id and self.private_key)

    def _app_jwt(self) -> str:
        if not self.is_configured():
            raise GitHubAppError("GitHub App credentials are not configured.")
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 9 * 60,
            "iss": self.app_id,
        }
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        bearer_token: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(
                    method,
                    f"{self.api_base_url}{path}",
                    headers=headers,
                    json=json_body,
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise GitHubAppError(f"GitHub API returned {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise GitHubAppError(f"GitHub API request failed: {exc}") from exc
        except ValueError as exc:
            raise GitHubAppError("GitHub API returned a non-JSON response.") from exc

        if not isinstance(payload, dict):
            raise GitHubAppError("GitHub API returned an unexpected response.")
        return payload

    async def create_installation_access_token(
        self,
        installation_id: str | int,
        *,
        repository_id: str | int | None = None,
    ) -> GitHubInstallationToken:
        body: dict[str, Any] = {}
        if repository_id is not None:
            try:
                body["repository_ids"] = [int(repository_id)]
            except (TypeError, ValueError):
                pass

        payload = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            bearer_token=self._app_jwt(),
            json_body=body,
        )
        token = str(payload.get("token") or "")
        if not token:
            raise GitHubAppError("GitHub installation token response did not include a token.")
        return GitHubInstallationToken(token=token, expires_at=payload.get("expires_at"))
