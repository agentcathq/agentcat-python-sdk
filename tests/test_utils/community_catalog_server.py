"""A community FastMCP server whose catalog is a code-mode-shaped transform.

Reproduces the mechanism of fastmcp's code mode WITHOUT pydantic-monty: a
``CatalogTransform`` subclass replaces the listing with one synthetic ``run``
tool whose body — exactly like code mode's ``execute`` and discovery tools —
fetches the real catalog via ``get_tool_catalog(ctx)`` (a nested ``tools/list``
with ``run_middleware=True``) and then drives a hidden backend tool via
``ctx.fastmcp.call_tool`` (a nested ``tools/call``). Everything the "sandbox"
observes — the catalog it was served, the arguments the bodies received, the
inner result as agent-authored code would see it — is recorded into the
``observed`` dict the factory takes, so tests can assert on the inside view as
well as the wire.

Two guard flags, deliberately separate: ``CatalogTransform`` is imported from
its module path (not in ``fastmcp.server.transforms.__all__``) and first
shipped mid-3.x, so only the catalog-fetch factory needs it — the composing
factories below use nothing newer than ``ctx.fastmcp.call_tool`` and must keep
running on every fastmcp release the community extra allows. A single guard
would silently shrink the daily version sweep's nested-call coverage to the
transform-era releases.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

try:
    from fastmcp import FastMCP as CommunityFastMCP
    from fastmcp.server.context import Context

    HAS_COMMUNITY_NESTING = True
except ImportError:
    CommunityFastMCP = None  # type: ignore
    Context = None  # type: ignore
    HAS_COMMUNITY_NESTING = False

try:
    from fastmcp.server.transforms.catalog import CatalogTransform
    from fastmcp.tools import Tool

    HAS_CATALOG_TRANSFORM = True
except ImportError:
    CatalogTransform = object  # type: ignore
    Tool = None  # type: ignore
    HAS_CATALOG_TRANSFORM = False


# The text `echo` refuses, so error-path tests have one failure shape.
BOOM_TEXT = "boom"
META_TOOL_NAME = "run"


class InnerToolFailed(RuntimeError):
    """Raised by the hidden `echo` on the sentinel text."""


def create_composing_server(
    observed: dict[str, Any], name: str = "composing"
) -> "FastMCP":
    """A plain FastMCP server whose LISTED tools call each other — no
    transform, no hidden catalog. This is the ordinary shape of nesting
    (`ctx.fastmcp.call_tool` from a tool body), available on every community
    fastmcp release, so the tests built on it run across the whole version
    sweep.

    - ``compose(text)`` calls ``echo`` once: one level of nesting.
    - ``fanout(a, b)`` runs two ``echo`` calls CONCURRENTLY via
      ``asyncio.gather``: the inner frames install and restore in the shared
      request state in completion order, which is exactly the interleaving
      the re-entrancy machinery has to survive.
    """
    import asyncio

    server = CommunityFastMCP(name)

    @server.tool
    async def echo(text: str) -> str:
        """Echo the text back."""
        observed.setdefault("delivered", []).append(("echo", {"text": text}))
        if text == BOOM_TEXT:
            raise InnerToolFailed("the inner tool failed")
        return f"echo:{text}"

    @server.tool
    async def compose(text: str, ctx: Context = None) -> str:  # type: ignore[assignment]
        """Call echo from inside another listed tool."""
        observed.setdefault("delivered", []).append(("compose", {"text": text}))
        inner = await ctx.fastmcp.call_tool("echo", {"text": text})
        observed["inner_structured"] = inner.structured_content
        return f"composed:{text}"

    @server.tool
    async def fanout(a: str, b: str, ctx: Context = None) -> str:  # type: ignore[assignment]
        """Call echo twice, concurrently."""
        results = await asyncio.gather(
            ctx.fastmcp.call_tool("echo", {"text": a}),
            ctx.fastmcp.call_tool("echo", {"text": b}),
        )
        observed["fanned"] = [r.structured_content for r in results]
        return f"fanned:{a},{b}"

    return server


def create_catalog_meta_server(
    observed: dict[str, Any],
    name: str = "catalog-meta",
    *,
    target: str = "echo",
    also_list: Any = None,
) -> "FastMCP":
    """A FastMCP server serving only ``run``, over hidden ``echo``/``compose``.

    ``run(program)`` fetches the catalog, then calls ``target`` with the
    program text — ``"echo"`` for one level of nesting, ``"compose"`` for two
    (compose itself calls echo). ``also_list``, if given, is an async callable
    the body awaits after the catalog fetch — the multi-server isolation tests
    hand it another server's ``list_tools``.
    """
    server = CommunityFastMCP(name)

    @server.tool
    async def echo(text: str) -> str:
        """Echo the text back."""
        observed.setdefault("delivered", []).append(("echo", {"text": text}))
        if text == BOOM_TEXT:
            raise InnerToolFailed("the inner tool failed")
        return f"echo:{text}"

    @server.tool
    async def compose(text: str, ctx: Context = None) -> str:  # type: ignore[assignment]
        """Call echo from inside another hidden tool (nesting of nesting)."""
        observed.setdefault("delivered", []).append(("compose", {"text": text}))
        inner = await ctx.fastmcp.call_tool("echo", {"text": text})
        return f"composed:{inner.structured_content}"

    class _MetaCatalog(CatalogTransform):
        """The catalog collapsed to one meta tool, code-mode style."""

        def __init__(self) -> None:
            super().__init__()
            self._meta_tool = self._make_meta_tool()

        def _make_meta_tool(self) -> Any:
            transform = self

            async def run(program: str, ctx: Context = None) -> str:  # type: ignore[assignment]
                """Run a program against the hidden catalog."""
                observed.setdefault("delivered", []).append(
                    (META_TOOL_NAME, {"program": program})
                )
                catalog = await transform.get_tool_catalog(ctx)
                observed["catalog"] = {
                    tool.name: sorted((tool.parameters or {}).get("properties", {}))
                    for tool in catalog
                }
                if also_list is not None:
                    observed["also_listed"] = list(await also_list())
                inner = await ctx.fastmcp.call_tool(target, {"text": program})
                observed["inner_text"] = "".join(
                    block.text
                    for block in inner.content
                    if hasattr(block, "text")
                )
                observed["inner_structured"] = inner.structured_content
                return f"ran:{program}"

            return Tool.from_function(fn=run, name=META_TOOL_NAME)

        async def transform_tools(self, tools: Any) -> Any:
            return [self._meta_tool]

        async def get_tool(self, name: str, call_next: Any, **kwargs: Any) -> Any:
            if name == META_TOOL_NAME:
                return self._meta_tool
            return await call_next(name, **kwargs)

    server.add_transform(_MetaCatalog())
    return server
