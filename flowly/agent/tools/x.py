"""X (Twitter) API integration tool for posting, searching, and reading timelines."""

import hashlib
import hmac
import time
import urllib.parse
import secrets
from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.base import Tool


class XTool(Tool):
    """
    Tool to interact with X (Twitter) API v2.

    Supports reading (Bearer Token) and writing (OAuth 1.0a).
    Get credentials at: https://developer.x.com/en/portal/dashboard
    """

    BASE_URL = "https://api.x.com/2"

    def __init__(
        self,
        bearer_token: str = "",
        api_key: str = "",
        api_secret: str = "",
        access_token: str = "",
        access_token_secret: str = "",
    ):
        self.bearer_token = bearer_token
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret

    @property
    def name(self) -> str:
        return "x"

    @property
    def description(self) -> str:
        return """Interact with X (Twitter): post tweets, search, read timelines, look up users.

Actions:
- post_tweet: Post a new tweet (requires OAuth 1.0a credentials)
- delete_tweet: Delete a tweet by ID (requires OAuth 1.0a credentials)
- search_tweets: Search recent tweets (last 7 days)
- get_timeline: Get a user's recent tweets by username
- get_user: Look up a user profile by username

Requires X API credentials configured in ~/.flowly/config.json"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The action to perform",
                    "enum": [
                        "post_tweet",
                        "delete_tweet",
                        "search_tweets",
                        "get_timeline",
                        "get_user",
                    ],
                },
                "text": {
                    "type": "string",
                    "description": "Tweet text (for post_tweet, max 280 chars)",
                },
                "tweet_id": {
                    "type": "string",
                    "description": "Tweet ID (for delete_tweet)",
                },
                "query": {
                    "type": "string",
                    "description": "Search query (for search_tweets)",
                },
                "username": {
                    "type": "string",
                    "description": "X username without @ (for get_timeline, get_user)",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results (5-100, default 10)",
                },
            },
            "required": ["action"],
        }

    # ── OAuth 1.0a signature ──

    def _oauth1_header(self, method: str, url: str, body: dict | None = None) -> str:
        """Build OAuth 1.0a Authorization header with HMAC-SHA1 signature."""
        oauth_params = {
            "oauth_consumer_key": self.api_key,
            "oauth_nonce": secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": self.access_token,
            "oauth_version": "1.0",
        }

        # Collect all params for signature base string
        all_params = {**oauth_params}
        if body:
            all_params.update(body)

        # Sort and encode
        sorted_params = "&".join(
            f"{_pct(k)}={_pct(v)}" for k, v in sorted(all_params.items())
        )

        base_string = f"{method.upper()}&{_pct(url)}&{_pct(sorted_params)}"
        signing_key = f"{_pct(self.api_secret)}&{_pct(self.access_token_secret)}"

        signature = hmac.new(
            signing_key.encode(), base_string.encode(), hashlib.sha1
        ).digest()

        import base64
        oauth_params["oauth_signature"] = base64.b64encode(signature).decode()

        header_parts = ", ".join(
            f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth_params.items())
        )
        return f"OAuth {header_parts}"

    def _has_bearer(self) -> bool:
        return bool(self.bearer_token)

    def _has_oauth1(self) -> bool:
        return bool(
            self.api_key
            and self.api_secret
            and self.access_token
            and self.access_token_secret
        )

    # ── HTTP helpers ──

    async def _get(
        self, endpoint: str, params: dict | None = None
    ) -> dict:
        """GET request with Bearer Token auth."""
        if not self._has_bearer():
            raise ValueError(
                "X Bearer Token not configured. "
                "Set integrations.x.bearerToken in ~/.flowly/config.json"
            )
        url = f"{self.BASE_URL}/{endpoint}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.bearer_token}"},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

    async def _post_oauth(
        self, endpoint: str, json_body: dict | None = None
    ) -> dict:
        """POST request with OAuth 1.0a auth."""
        if not self._has_oauth1():
            raise ValueError(
                "X OAuth 1.0a credentials not configured. "
                "Set integrations.x.apiKey, apiSecret, accessToken, "
                "accessTokenSecret in ~/.flowly/config.json"
            )
        url = f"{self.BASE_URL}/{endpoint}"
        auth_header = self._oauth1_header("POST", url)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                json=json_body,
                headers={
                    "Authorization": auth_header,
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

    async def _delete_oauth(self, endpoint: str) -> dict:
        """DELETE request with OAuth 1.0a auth."""
        if not self._has_oauth1():
            raise ValueError(
                "X OAuth 1.0a credentials not configured. "
                "Set integrations.x.apiKey, apiSecret, accessToken, "
                "accessTokenSecret in ~/.flowly/config.json"
            )
        url = f"{self.BASE_URL}/{endpoint}"
        auth_header = self._oauth1_header("DELETE", url)
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                url,
                headers={"Authorization": auth_header},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()

    # ── Actions ──

    async def execute(self, action: str, **kwargs: Any) -> str:
        """Execute an X API action."""
        try:
            if action == "post_tweet":
                return await self._post_tweet(kwargs.get("text", ""))
            elif action == "delete_tweet":
                return await self._delete_tweet(kwargs.get("tweet_id", ""))
            elif action == "search_tweets":
                return await self._search_tweets(
                    kwargs.get("query", ""),
                    kwargs.get("max_results", 10),
                )
            elif action == "get_timeline":
                return await self._get_timeline(
                    kwargs.get("username", ""),
                    kwargs.get("max_results", 10),
                )
            elif action == "get_user":
                return await self._get_user(kwargs.get("username", ""))
            else:
                return f"Unknown action: {action}"
        except httpx.HTTPStatusError as e:
            return f"X API error: {e.response.status_code} - {e.response.text}"
        except ValueError as e:
            return str(e)
        except Exception as e:
            logger.error(f"X tool error: {e}")
            return f"Error: {str(e)}"

    async def _post_tweet(self, text: str) -> str:
        """Post a tweet."""
        if not text:
            return "Error: text is required"
        if len(text) > 280:
            return f"Error: tweet is {len(text)} chars, max 280"

        result = await self._post_oauth("tweets", {"text": text})
        data = result.get("data", {})
        tweet_id = data.get("id", "unknown")
        return f"Tweet posted!\nID: {tweet_id}\nURL: https://x.com/i/status/{tweet_id}"

    async def _delete_tweet(self, tweet_id: str) -> str:
        """Delete a tweet."""
        if not tweet_id:
            return "Error: tweet_id is required"

        result = await self._delete_oauth(f"tweets/{tweet_id}")
        deleted = result.get("data", {}).get("deleted", False)
        if deleted:
            return f"Tweet {tweet_id} deleted."
        return f"Could not delete tweet {tweet_id}: {result}"

    async def _search_tweets(self, query: str, max_results: int = 10) -> str:
        """Search recent tweets (last 7 days)."""
        if not query:
            return "Error: query is required"

        max_results = max(10, min(max_results, 100))
        result = await self._get(
            "tweets/search/recent",
            params={
                "query": query,
                "max_results": max_results,
                "tweet.fields": "created_at,author_id,public_metrics,text",
                "expansions": "author_id",
                "user.fields": "username,name",
            },
        )

        tweets = result.get("data", [])
        if not tweets:
            return f"No tweets found for: {query}"

        # Build username lookup from includes
        users = {}
        for u in result.get("includes", {}).get("users", []):
            users[u["id"]] = u.get("username", u.get("name", "unknown"))

        lines = [f"Search results for '{query}':\n"]
        for tweet in tweets:
            author = users.get(tweet.get("author_id", ""), "unknown")
            metrics = tweet.get("public_metrics", {})
            likes = metrics.get("like_count", 0)
            retweets = metrics.get("retweet_count", 0)
            replies = metrics.get("reply_count", 0)
            text = tweet.get("text", "")
            created = tweet.get("created_at", "")[:10]

            lines.append(f"@{author} ({created})")
            lines.append(f"  {text}")
            lines.append(f"  Likes: {likes} | Retweets: {retweets} | Replies: {replies}")
            lines.append(f"  https://x.com/{author}/status/{tweet['id']}")
            lines.append("")

        return "\n".join(lines)

    async def _get_timeline(self, username: str, max_results: int = 10) -> str:
        """Get a user's recent tweets."""
        if not username:
            return "Error: username is required"

        username = username.lstrip("@")
        max_results = max(5, min(max_results, 100))

        # First get user ID
        user_data = await self._get(
            f"users/by/username/{username}",
            params={"user.fields": "name,public_metrics"},
        )
        user = user_data.get("data")
        if not user:
            return f"User @{username} not found"

        user_id = user["id"]
        display_name = user.get("name", username)

        # Then get their tweets
        result = await self._get(
            f"users/{user_id}/tweets",
            params={
                "max_results": max_results,
                "tweet.fields": "created_at,public_metrics,text",
                "exclude": "replies,retweets",
            },
        )

        tweets = result.get("data", [])
        if not tweets:
            return f"No recent tweets from @{username}"

        lines = [f"Recent tweets from @{username} ({display_name}):\n"]
        for tweet in tweets:
            metrics = tweet.get("public_metrics", {})
            likes = metrics.get("like_count", 0)
            retweets = metrics.get("retweet_count", 0)
            text = tweet.get("text", "")
            created = tweet.get("created_at", "")[:10]

            lines.append(f"[{created}] {text}")
            lines.append(f"  Likes: {likes} | Retweets: {retweets}")
            lines.append(f"  https://x.com/{username}/status/{tweet['id']}")
            lines.append("")

        return "\n".join(lines)

    async def _get_user(self, username: str) -> str:
        """Look up a user profile."""
        if not username:
            return "Error: username is required"

        username = username.lstrip("@")
        result = await self._get(
            f"users/by/username/{username}",
            params={
                "user.fields": "name,description,public_metrics,created_at,location,verified",
            },
        )

        user = result.get("data")
        if not user:
            return f"User @{username} not found"

        metrics = user.get("public_metrics", {})
        lines = [
            f"@{user.get('username', username)} - {user.get('name', '')}",
            "",
        ]
        if user.get("description"):
            lines.append(f"Bio: {user['description']}")
        if user.get("location"):
            lines.append(f"Location: {user['location']}")
        if user.get("created_at"):
            lines.append(f"Joined: {user['created_at'][:10]}")
        lines.append(
            f"Followers: {metrics.get('followers_count', 0):,} | "
            f"Following: {metrics.get('following_count', 0):,} | "
            f"Tweets: {metrics.get('tweet_count', 0):,}"
        )
        lines.append(f"URL: https://x.com/{username}")

        return "\n".join(lines)


def _pct(value: str) -> str:
    """Percent-encode a string per RFC 3986."""
    return urllib.parse.quote(str(value), safe="")
