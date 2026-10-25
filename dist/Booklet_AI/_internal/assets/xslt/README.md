# OMML2MML.XSL — Required XSLT Stylesheet

This directory must contain `OMML2MML.XSL` — the official Microsoft
stylesheet that converts Office Math Markup Language (OMML) to MathML.

## How to obtain

The file ships with every Microsoft Office installation. Typical paths:

- `C:\Program Files\Microsoft Office\root\Office16\OMML2MML.XSL`
- `C:\Program Files (x86)\Microsoft Office\root\Office16\OMML2MML.XSL`
- `C:\Program Files\Microsoft Office\Office16\OMML2MML.XSL`

Copy `OMML2MML.XSL` into this directory. The `DocxMathExtractor` will
also search the above Office paths automatically as a fallback.

## Redistribution notice

Before bundling this file inside a distributed `.exe`, confirm the
redistribution terms of the stylesheet with Microsoft's licensing.
For development and internal use, copying from a local Office
installation is acceptable.
