# Handoff: Outlook MCP attachment support

## Why this exists

The original `inventory-mailbox-scan-4h` SKILL.md was built around the Outlook MCP (the Microsoft 365 connector that exposes `outlook_email_search` and `read_resource`). The plan was: search messages, then call `read_resource` with an attachment URI to pull PDF bytes for local parsing.

That plan doesn't work, because the Outlook MCP server's `read_resource` URI scheme has no attachment route. It documents only:

- `mail:///messages/{messageId}[?owner={email}]`
- `mail:///folders/{folderId}`

When you append `/attachments` (e.g. `mail:///messages/{id}/attachments?owner=...`), the server matches the message ID, ignores the trailing path, and returns the same metadata-only response — including `hasAttachments: true` but no bytes, no `contentBytes`, no `attachments` array.

We worked around this by talking to Microsoft Graph directly from the script (see `scripts/cowork_graph_scan.py`). That works, but it duplicates auth and burns the user's tenant/client/secret into the local Cowork environment. Closing this gap in the MCP itself would let any Cowork plugin (not just ours) read mail attachments.

## What needs to change

The Outlook MCP needs to teach `read_resource` two new URI shapes:

1. **List attachments on a message:**
   ```
   mail:///messages/{messageId}/attachments[?owner={email}]
   ```
   Returns the `value` array from Graph's `GET /users/{owner}/messages/{id}/attachments?$select=id,name,contentType,size`. One entry per attachment. The `@odata.type` annotation should be preserved so callers can distinguish `#microsoft.graph.fileAttachment` from `itemAttachment`/`referenceAttachment`.

2. **Read a single attachment's bytes:**
   ```
   mail:///messages/{messageId}/attachments/{attachmentId}[?owner={email}]
   ```
   For `fileAttachment`, return either:
   - the raw bytes of `GET /users/{owner}/messages/{id}/attachments/{att-id}/$value`, OR
   - a JSON wrapper `{"contentType": "...", "name": "...", "data": "<base64>"}` if the MCP framework requires structured responses.

   For `itemAttachment` (forwarded `.eml`-style nesting) and `referenceAttachment` (OneDrive/SharePoint pointers), it's fine to return only metadata + a 415-equivalent error code; the caller can fall back to per-type handling.

## Where to make the change

The MCP server identifier in our session was `33b8cc53-0682-4e0b-b76c-8adb826bfc96`. That's an opaque per-install ID, so finding the source means following one of these:

1. Look at Cowork's installed-MCPs list (`mcp-registry` MCP, `list_connectors`) for the connector whose tools include `outlook_email_search` and `read_resource`. Its registry entry should carry a homepage / repo URL.
2. If it's an Anthropic-shipped MCP, the change is upstream and out of our reach — file a feature request through the Cowork feedback path noting the missing URI scheme.
3. If it's a community/forked MCP, fork and add the two URI patterns above. Most existing Outlook MCP implementations already wrap `httpx`/`microsoft-graph-core` and just need new routes wired in.

## Alternative: swap to a different MCP

If extending the current MCP isn't viable, the cleaner path is to use one that already supports attachments. The official Microsoft Graph MCP (when available) exposes attachments at a `graph://users/{user}/messages/{id}/attachments` URI; swapping our calls to its naming would preserve everything except the URI prefix. The `cowork_graph_scan.py` script we wrote does the same Graph calls already — that's the existence proof that the data model fits.

## Status

- Workaround in place: `scripts/cowork_graph_scan.py` + the updated `inventory-mailbox-scan-4h` SKILL.md.
- This doc serves as the handoff for whoever later wants to remove the workaround and let any plugin in Cowork read mail attachments through the standard MCP surface.
