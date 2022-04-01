"""Main module for O2MConverter package.

O2MConverter
Copyright 2020-2022 Aleksi Ikkala, Anton Sobinov
https://github.com/aikkala/O2MConverter
"""

__all__ = ['Utils', 'O2MConverter', 'O42MConverter']


from . import Utils
from .O2MConverter import Converter
from .O42MConverter import Converter4
