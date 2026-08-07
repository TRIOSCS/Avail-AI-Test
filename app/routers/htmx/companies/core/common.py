"""routers/htmx/companies/core/common.py — shared router shim for the core subpackage.

W4.8 split of the 1,407-line app/routers/htmx/companies/core.py — pure structural
move: URLs and behavior unchanged. The single APIRouter every route in the companies
package attaches to is DEFINED in app.routers.htmx.companies.__init__ (no prefix;
mounted by app.main); this module only re-exports it so core's submodules can
``from .common import router`` (mirroring app/routers/sightings/common.py).
Registration order is assembled in core/__init__.

Called by: .lists, .lifecycle, .editing (router import), core/__init__ (re-export)
Depends on: app.routers.htmx.companies (parent package router)
"""

from .. import router

__all__ = ["router"]
