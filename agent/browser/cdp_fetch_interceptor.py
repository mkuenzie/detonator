import base64


class CDPFetchInterceptor:
    """Fetch.requestPaused → Fetch.getResponseBody → Fetch.continueResponse.

    Scoped to resourceType=Document, requestStage=Response, so only main-frame
    and iframe document fetches are paused. Feeds the same NetworkCapture sink
    as CDPResponseTap.

    Invariant: every paused request MUST reach continueResponse, even on body
    read failure — a leaked pause hangs the navigation.
    """

    async def _attach_page(self, page):
        session = await self._context.new_cdp_session(page)
        session.on("Fetch.requestPaused",
                   lambda ev: self._schedule(self._on_paused(ev, session)))
        await session.send("Fetch.enable", {
            "patterns": [{
                "resourceType": "Document",
                "requestStage":  "Response",
            }]
        })

    async def _on_paused(self, ev, session):
        req_id      = ev["requestId"]              # Fetch interception ID
        network_id  = ev.get("networkId", "")      # correlates with Network domain
        url         = ev["request"]["url"]
        method      = ev["request"].get("method", "GET").upper()
        status      = ev.get("responseStatusCode", 0)
        headers     = {h["name"].lower(): h["value"]
                       for h in ev.get("responseHeaders", [])}
        mime        = (headers.get("content-type", "") or "").split(";", 1)[0].strip() or None
        resource_ty = (ev.get("resourceType") or "").lower() or None

        body, outcome, reason = None, "ok", None
        try:
            result = await session.send("Fetch.getResponseBody", {"requestId": req_id})
            body = (base64.b64decode(result["body"])
                    if result.get("base64Encoded")
                    else result.get("body", "").encode("utf-8", errors="replace"))
        except Exception as exc:
            outcome, reason = "error", str(exc)

        # CRITICAL: always continue, even on failure.
        try:
            await session.send("Fetch.continueResponse", {"requestId": req_id})
        except Exception:
            pass

        tagged = network_id or req_id
        headers = {h["name"].lower(): h["value"]
           for h in ev.get("responseHeaders", [])}
        if body is not None and outcome == "ok":
            await self._sink.record_response(
                request_id=tagged, url=url, method=method, status=status,
                mime_type=mime, resource_type=resource_ty, frame_url=None,
                remote_address=None, response_headers=headers,
                body=body, outcome="ok",
            )
        else:
            await self._sink.record_failure(
                request_id=tagged, url=url, method=method,
                outcome=outcome, reason=reason,
            )