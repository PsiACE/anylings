from datetime import date

project = "anylings"
author = "anylings contributors"
year = date.today().year
copyright = f"{year}, {author}"

extensions = [
    "myst_parser",
    "numpydoc",
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "sphinx_design",
]

root_doc = "index"
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
exclude_patterns = ["_build"]

autosummary_generate = True
autodoc_typehints = "description"
numpydoc_show_class_members = False
myst_heading_anchors = 3
myst_enable_extensions = ["colon_fence"]

html_theme = "pydata_sphinx_theme"
html_title = "anylings"
html_baseurl = "https://anylings.com/"
html_extra_path = ["CNAME"]
html_theme_options = {
    "github_url": "https://github.com/PsiACE/anylings",
    "navigation_with_keys": True,
    "show_toc_level": 2,
    "use_edit_page_button": True,
}
html_context = {
    "github_user": "PsiACE",
    "github_repo": "anylings",
    "github_version": "main",
    "doc_path": "docs",
}
