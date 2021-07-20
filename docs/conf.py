import sprockets_statsd

project = 'sprockets-statsd'
version = sprockets_statsd.version
copyright = '2021 AWeber Communications, Inc.'
html_theme = 'pyramid'
extensions = []

# https://www.sphinx-doc.org/en/master/usage/extensions/autodoc.html
extensions.append('sphinx.ext.autodoc')

# https://www.sphinx-doc.org/en/master/usage/extensions/intersphinx.html
extensions.append('sphinx.ext.intersphinx')
intersphinx_mapping = {
    'python': ('https://docs.python.org/3/', None),
    'tornado': ('https://www.tornadoweb.org/en/branch6.0/', None),
}

# https://www.sphinx-doc.org/en/master/usage/extensions/extlinks.html
extensions.append('sphinx.ext.extlinks')
extlinks = {
    'issue': ("https://github.com/sprockets/sprockets-statsd/issues/%s", "#"),
    'tag': ("https://github.com/sprockets/sprockets-statsd/compare/%s", "%s"),
}

# https://pypi.org/project/sphinx-autodoc-typehints/
extensions.append('sphinx_autodoc_typehints')
