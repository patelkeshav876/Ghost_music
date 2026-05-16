import sys
try:
    import py_tgcalls
    print("py_tgcalls module found")
    print("Available:", dir(py_tgcalls))
except ImportError as e:
    print(f"Error: {e}")

try:
    from pytgcalls import PyTgCalls
    print("PyTgCalls imported from pytgcalls")
except ImportError:
    print("Could not import from pytgcalls")

try:
    from ntgcalls import NTgCalls
    print("NTgCalls imported from ntgcalls")
except ImportError:
    print("Could not import from ntgcalls")
