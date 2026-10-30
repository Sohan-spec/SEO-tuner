"""Package this folder as seofixer-preview.vsix (a .vsix is a zip with a manifest). Run: python3 build_vsix.py"""
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="seofixer-preview" Version="0.1.0" Publisher="seofixer"/>
    <DisplayName>seofixer preview</DisplayName>
    <Description xml:space="preserve">Lets the seofixer CLI open its local preview in the editor's built-in browser.</Description>
    <Categories>Other</Categories>
    <Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="^1.80.0"/></Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets>
</PackageManifest>"""
TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension=".json" ContentType="application/json"/><Default Extension=".js" ContentType="application/javascript"/><Default Extension=".vsixmanifest" ContentType="text/xml"/></Types>"""

with zipfile.ZipFile(HERE / "seofixer-preview.vsix", "w", zipfile.ZIP_DEFLATED) as vsix:
    vsix.writestr("extension.vsixmanifest", MANIFEST)
    vsix.writestr("[Content_Types].xml", TYPES)
    for name in ("package.json", "extension.js"):
        vsix.write(HERE / name, f"extension/{name}")
print(HERE / "seofixer-preview.vsix")
