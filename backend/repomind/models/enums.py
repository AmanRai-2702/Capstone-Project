from enum import StrEnum


class Language(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    HTML = "html"
    CSS = "css"
    UNKNOWN = "unknown"


class EntityType(StrEnum):
    REPOSITORY = "repository"
    DIRECTORY = "directory"
    FILE = "file"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    HTML_ELEMENT = "html_element"
    CSS_SELECTOR = "css_selector"
    API_ENDPOINT = "api_endpoint"


class RelationType(StrEnum):
    CONTAINS = "CONTAINS"
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    DEFINES = "DEFINES"
    REFERENCES = "REFERENCES"
    TRIGGERS = "TRIGGERS"    # HTML element -> JS handler
    RENDERS = "RENDERS"
    STYLES = "STYLES"        # CSS selector -> HTML element
    REQUESTS = "REQUESTS"    # JS function -> API endpoint
    HANDLES = "HANDLES"      # Python function -> API endpoint
    IMPLEMENTS = "IMPLEMENTS"
    DEPENDS_ON = "DEPENDS_ON"


class RelationKind(StrEnum):
    DETECTED = "detected"    # directly visible in code
    INFERRED = "inferred"    # we're guessing from patterns
    


class FileCategory(StrEnum):
    SOURCE = "source"    # Python/JS/HTML/CSS we will analyze
    DOC = "doc"          # README, markdown, text docs
    CONFIG = "config"    # json, yaml, requirements.txt, Dockerfile...
    OTHER = "other"      # kept in the inventory but not analyzed