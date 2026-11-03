# LMC Toolchain – Installation Guide

This project is a standard Python package. The normal installation flow is to clone or download the source tree, install it with pip, and then run either the `lmcc` command or the IDE entry point.

## Requirements

- Python 3.9 or newer
- `pip`
- For the GUI IDE: Tkinter support in your Python installation
- Optional: QEMU if you want to run the generated AArch64 image on an x86-64 machine

Check your Python and pip versions:

```bash
python --version
python -m pip --version
```

If you are on MSYS2, install Python via MSYS2 as needed:

```bash
pacman -S mingw-w64-ucrt-x86_64-python
```

If you are using a system Python that blocks installs because of PEP 668, the MSYS2 workaround is:

```bash
python -m pip install --break-system-packages .
```

## Install from the source tree

From the repository root, run:

```bash
python -m pip install .
```

For local development, use an editable install instead:

```bash
python -m pip install -e .
```

This project defines the following console entry points in `pyproject.toml`:

- `lmcc` for the compiler
- `lmc-ide` for the GUI IDE

## Verify the installation

Check that the package is installed and the console script is available:

```bash
python -m pip show lmc-toolchain
lmcc --help
```

If the script is not on your PATH, you can run the package directly instead:

```bash
python -m lmc --help
```

## Run the compiler

From the project directory or after installation, compile a program with:

```bash
lmcc path/to/program.lmc
```

You can also invoke the module directly:

```bash
python -m lmc path/to/program.lmc
```

## Run the IDE

Installed entry point:

```bash
lmc-ide
```

Or from a checkout without installing:

```bash
python lmc-ide.pyw
```

If you want to start the IDE via the package entry point without installation, this also works:

```bash
python -m lmc ide
```

## Optional AArch64 execution environment

This project builds AArch64 bare-metal images, but that does not mean your host machine must be AArch64. The compiler itself is installed and run on the host OS.

If you want to execute the compiled image on an x86-64 Windows or Linux machine, install QEMU:

```bash
qemu-system-aarch64 --version
```

If you are on a native AArch64 machine, you can also run the generated image directly there.

## Troubleshooting

### `error: externally-managed-environment`

Use:

```bash
python -m pip install --break-system-packages .
```

### `No module named tkinter` or the IDE fails to start

Install the Tk GUI bindings for your Python environment:

- Windows/macOS: install Python from python.org, which includes Tk
- Ubuntu/Debian: `sudo apt install python3-tk`
- Fedora: `sudo dnf install python3-tkinter`

### `lmcc: command not found`

Your installation may not have completed successfully, or the venv/ environment is not active. Try:

```bash
python -m pip install .
```

Then check:

```bash
python -m pip show lmc-toolchain
```

### Wrong directory / missing files

Make sure you are in the project root and that it contains these files:

```bash
ls
```

You should see at least `pyproject.toml`, `setup.cfg`, `README.md`, and the `src/` directory.

## Quick reference

```bash
cd /path/to/LMIDE
python -m pip install -e .
lmcc --help
lmc-ide
```

For a production install from a release checkout:

```bash
cd /path/to/LMIDE
python -m pip install .
```

This is the supported installation flow for the current repository layout.