from ngapp import AppConfig

from . import __version__
from .app import NGSolveGui
from .branding import PRODUCT_DESCRIPTION, PRODUCT_NAME

config = AppConfig(
    name=PRODUCT_NAME,
    version=__version__,
    python_class=NGSolveGui,
    frontend_pip_dependencies=["ngsolve", "ngsolve_webgpu"],
    description=PRODUCT_DESCRIPTION,
)
