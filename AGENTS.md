# Python coding guideline

 - Entry functions at the top of files, only then helpers functions. Types top entry functions.
 - Make sure code is always type checked by running `pyright`
 - Any configuration should be managed via CLI argument or environment variables. All of that is retrieved and checked in bot/__main__.py.
 - Pydoc for functions should always be of the format:
 - Use string interpolation, i.e. f"hello {user}"

```python
def myfunc(arg1: int, arg2: str) -> str:
"""
My func description.

Args:
- arg1 - description of the first argument
- arg2 - description of the second argument

Returns: description of returned value
"""
```

Functions that no arguments only need a function description.

