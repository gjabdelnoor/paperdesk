from .viewport import ViewportExtension
from krita import Krita
Krita.instance().addExtension(ViewportExtension(Krita.instance()))
