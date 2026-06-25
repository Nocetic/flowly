"""Trello integration tool for managing boards, lists, and cards."""

from typing import Any

import httpx
from loguru import logger

from flowly.agent.tools.base import Tool


class TrelloTool(Tool):
    """
    Tool to interact with Trello boards, lists, and cards.

    Requires TRELLO_API_KEY and TRELLO_TOKEN environment variables,
    or pass them via config.

    Get credentials at: https://trello.com/app-key
    """

    BASE_URL = "https://api.trello.com/1"

    def __init__(self, api_key: str | None = None, token: str | None = None):
        self.api_key = api_key
        self.token = token

    @property
    def name(self) -> str:
        return "trello"

    @property
    def description(self) -> str:
        return """Manage Trello boards, lists, and cards.

Actions:
- list_boards: Get all your Trello boards
- list_lists: Get all lists in a board
- list_cards: Get all cards in a list
- get_card: Get details of a specific card
- create_card: Create a new card in a list
- update_card: Update card name, description, or move to another list
- add_comment: Add a comment to a card
- archive_card: Archive (close) a card
- search: Search for cards across all boards

Requires Trello API key and token. Get them at https://trello.com/app-key"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "The action to perform",
                    "enum": [
                        "list_boards",
                        "list_lists",
                        "list_cards",
                        "get_card",
                        "create_card",
                        "update_card",
                        "add_comment",
                        "archive_card",
                        "search"
                    ]
                },
                "board_id": {
                    "type": "string",
                    "description": "Board ID (for list_lists, list_cards)"
                },
                "list_id": {
                    "type": "string",
                    "description": "List ID (for list_cards, create_card)"
                },
                "card_id": {
                    "type": "string",
                    "description": "Card ID (for get_card, update_card, add_comment, archive_card)"
                },
                "name": {
                    "type": "string",
                    "description": "Card name (for create_card, update_card)"
                },
                "description": {
                    "type": "string",
                    "description": "Card description (for create_card, update_card)"
                },
                "comment": {
                    "type": "string",
                    "description": "Comment text (for add_comment)"
                },
                "query": {
                    "type": "string",
                    "description": "Search query (for search)"
                },
                "due_date": {
                    "type": "string",
                    "description": "Due date in ISO format (for create_card, update_card)"
                },
                "labels": {
                    "type": "string",
                    "description": "Comma-separated label IDs (for create_card, update_card)"
                }
            },
            "required": ["action"]
        }

    def _get_auth_params(self) -> dict[str, str]:
        """Get authentication query parameters."""
        import os
        api_key = self.api_key or os.environ.get("TRELLO_API_KEY", "")
        token = self.token or os.environ.get("TRELLO_TOKEN", "")

        if not api_key or not token:
            raise ValueError(
                "Trello credentials not configured. "
                "Set TRELLO_API_KEY and TRELLO_TOKEN environment variables, "
                "or configure in ~/.flowly/config.json"
            )

        return {"key": api_key, "token": token}

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        data: dict | None = None
    ) -> dict | list:
        """Make an authenticated request to Trello API."""
        url = f"{self.BASE_URL}/{endpoint}"
        auth_params = self._get_auth_params()

        all_params = {**auth_params, **(params or {})}

        async with httpx.AsyncClient() as client:
            if method == "GET":
                response = await client.get(url, params=all_params, timeout=30)
            elif method == "POST":
                response = await client.post(url, params=all_params, data=data, timeout=30)
            elif method == "PUT":
                response = await client.put(url, params=all_params, data=data, timeout=30)
            elif method == "DELETE":
                response = await client.delete(url, params=all_params, timeout=30)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            return response.json()

    async def execute(self, action: str, **kwargs: Any) -> str:
        """Execute a Trello action."""
        try:
            if action == "list_boards":
                return await self._list_boards()
            elif action == "list_lists":
                return await self._list_lists(kwargs.get("board_id", ""))
            elif action == "list_cards":
                list_id = kwargs.get("list_id", "")
                board_id = kwargs.get("board_id", "")
                return await self._list_cards(list_id=list_id, board_id=board_id)
            elif action == "get_card":
                return await self._get_card(kwargs.get("card_id", ""))
            elif action == "create_card":
                return await self._create_card(
                    list_id=kwargs.get("list_id", ""),
                    name=kwargs.get("name", ""),
                    description=kwargs.get("description"),
                    due_date=kwargs.get("due_date"),
                    labels=kwargs.get("labels")
                )
            elif action == "update_card":
                return await self._update_card(
                    card_id=kwargs.get("card_id", ""),
                    name=kwargs.get("name"),
                    description=kwargs.get("description"),
                    list_id=kwargs.get("list_id"),
                    due_date=kwargs.get("due_date")
                )
            elif action == "add_comment":
                return await self._add_comment(
                    card_id=kwargs.get("card_id", ""),
                    comment=kwargs.get("comment", "")
                )
            elif action == "archive_card":
                return await self._archive_card(kwargs.get("card_id", ""))
            elif action == "search":
                return await self._search(kwargs.get("query", ""))
            else:
                return f"Unknown action: {action}"
        except httpx.HTTPStatusError as e:
            return f"Trello API error: {e.response.status_code} - {e.response.text}"
        except ValueError as e:
            return str(e)
        except Exception as e:
            logger.error(f"Trello error: {e}")
            return f"Error: {str(e)}"

    async def _list_boards(self) -> str:
        """List all boards."""
        boards = await self._request("GET", "members/me/boards", params={"fields": "name,id,url"})

        if not boards:
            return "No boards found."

        lines = ["📋 Your Trello Boards:\n"]
        for board in boards:
            lines.append(f"• {board['name']}")
            lines.append(f"  ID: {board['id']}")
            lines.append(f"  URL: {board['url']}\n")

        return "\n".join(lines)

    async def _list_lists(self, board_id: str) -> str:
        """List all lists in a board."""
        if not board_id:
            return "Error: board_id is required"

        lists = await self._request("GET", f"boards/{board_id}/lists", params={"fields": "name,id"})

        if not lists:
            return "No lists found in this board."

        lines = ["📝 Lists in board:\n"]
        for lst in lists:
            lines.append(f"• {lst['name']} (ID: {lst['id']})")

        return "\n".join(lines)

    async def _list_cards(self, list_id: str = "", board_id: str = "") -> str:
        """List cards in a list or board."""
        if list_id:
            cards = await self._request(
                "GET",
                f"lists/{list_id}/cards",
                params={"fields": "name,id,desc,due,labels"}
            )
        elif board_id:
            cards = await self._request(
                "GET",
                f"boards/{board_id}/cards",
                params={"fields": "name,id,desc,due,labels,idList"}
            )
        else:
            return "Error: list_id or board_id is required"

        if not cards:
            return "No cards found."

        lines = ["🃏 Cards:\n"]
        for card in cards:
            lines.append(f"• {card['name']}")
            lines.append(f"  ID: {card['id']}")
            if card.get('desc'):
                desc = card['desc'][:100] + "..." if len(card.get('desc', '')) > 100 else card['desc']
                lines.append(f"  Description: {desc}")
            if card.get('due'):
                lines.append(f"  Due: {card['due']}")
            if card.get('labels'):
                label_names = [l.get('name', l.get('color', '')) for l in card['labels']]
                lines.append(f"  Labels: {', '.join(label_names)}")
            lines.append("")

        return "\n".join(lines)

    async def _get_card(self, card_id: str) -> str:
        """Get details of a specific card."""
        if not card_id:
            return "Error: card_id is required"

        card = await self._request(
            "GET",
            f"cards/{card_id}",
            params={"fields": "name,desc,due,labels,url", "actions": "commentCard", "actions_limit": 5}
        )

        lines = [f"🃏 Card: {card['name']}\n"]
        lines.append(f"ID: {card['id']}")
        lines.append(f"URL: {card['url']}")

        if card.get('desc'):
            lines.append(f"\nDescription:\n{card['desc']}")

        if card.get('due'):
            lines.append(f"\nDue: {card['due']}")

        if card.get('labels'):
            label_names = [l.get('name', l.get('color', '')) for l in card['labels']]
            lines.append(f"\nLabels: {', '.join(label_names)}")

        if card.get('actions'):
            lines.append("\nRecent comments:")
            for action in card['actions'][:5]:
                if action['type'] == 'commentCard':
                    lines.append(f"  • {action['data']['text'][:100]}")

        return "\n".join(lines)

    async def _create_card(
        self,
        list_id: str,
        name: str,
        description: str | None = None,
        due_date: str | None = None,
        labels: str | None = None
    ) -> str:
        """Create a new card."""
        if not list_id:
            return "Error: list_id is required"
        if not name:
            return "Error: name is required"

        data = {"idList": list_id, "name": name}

        if description:
            data["desc"] = description
        if due_date:
            data["due"] = due_date
        if labels:
            data["idLabels"] = labels

        card = await self._request("POST", "cards", data=data)

        return f"✅ Card created!\n\nName: {card['name']}\nID: {card['id']}\nURL: {card['url']}"

    async def _update_card(
        self,
        card_id: str,
        name: str | None = None,
        description: str | None = None,
        list_id: str | None = None,
        due_date: str | None = None
    ) -> str:
        """Update a card."""
        if not card_id:
            return "Error: card_id is required"

        data = {}
        if name:
            data["name"] = name
        if description is not None:
            data["desc"] = description
        if list_id:
            data["idList"] = list_id
        if due_date:
            data["due"] = due_date

        if not data:
            return "Error: Nothing to update. Provide name, description, list_id, or due_date."

        card = await self._request("PUT", f"cards/{card_id}", data=data)

        updates = []
        if name:
            updates.append(f"name → {name}")
        if description is not None:
            updates.append("description updated")
        if list_id:
            updates.append("moved to new list")
        if due_date:
            updates.append(f"due date → {due_date}")

        return f"✅ Card updated!\n\nChanges: {', '.join(updates)}\nURL: {card['url']}"

    async def _add_comment(self, card_id: str, comment: str) -> str:
        """Add a comment to a card."""
        if not card_id:
            return "Error: card_id is required"
        if not comment:
            return "Error: comment is required"

        result = await self._request(
            "POST",
            f"cards/{card_id}/actions/comments",
            data={"text": comment}
        )

        return f"✅ Comment added to card!"

    async def _archive_card(self, card_id: str) -> str:
        """Archive a card."""
        if not card_id:
            return "Error: card_id is required"

        card = await self._request("PUT", f"cards/{card_id}", data={"closed": "true"})

        return f"✅ Card archived: {card['name']}"

    async def _search(self, query: str) -> str:
        """Search for cards."""
        if not query:
            return "Error: query is required"

        results = await self._request(
            "GET",
            "search",
            params={
                "query": query,
                "modelTypes": "cards",
                "cards_limit": 10,
                "card_fields": "name,id,desc,url"
            }
        )

        cards = results.get("cards", [])

        if not cards:
            return f"No cards found for query: {query}"

        lines = [f"🔍 Search results for '{query}':\n"]
        for card in cards:
            lines.append(f"• {card['name']}")
            lines.append(f"  ID: {card['id']}")
            lines.append(f"  URL: {card['url']}")
            if card.get('desc'):
                desc = card['desc'][:80] + "..." if len(card.get('desc', '')) > 80 else card['desc']
                lines.append(f"  {desc}")
            lines.append("")

        return "\n".join(lines)
