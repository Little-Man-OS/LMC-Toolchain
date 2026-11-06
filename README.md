# LMC toolchain

A compiler and IDE for LMC (Little Man Computer) that builds `kernel8.img`, a flat binary for
the Raspberry Pi 3B that runs with no operating system. The machine code is generated directly,
so there is no assembler or linker step. It has no dependencies beyond Python 3.8 or newer.

## Install

    pip install lmc_toolchain-0.4.0-py3-none-any.whl

or from the source folder, `pip install .` (use `pip install -e .` while you are changing it).
That gives you two commands, `lmcc` for the compiler and `lmc-ide` for the IDE. It needs
Python 3.9 or newer and has no other dependencies. The IDE needs Python with Tk, which the
normal Windows and macOS installers include. Running programs needs QEMU
(`qemu-system-aarch64`), and everything else works without it.

If your shell says `lmc-ide` or `lmcc` is not recognised, pip put them in a folder that is not on
your PATH, which is common on Windows. These always work instead:

    python -m lmc ide              open the IDE
    python -m lmc prog.lmc         run the compiler

### MSYS2

Use the Python from the same MSYS2 environment as your QEMU, which is usually the UCRT64 shell
(or MINGW64). In that shell:

    pip install --break-system-packages lmc_toolchain-0.4.0-py3-none-any.whl
    python -m lmc ide

`--break-system-packages` is only there because MSYS2 manages its Python with pacman, and this
package just adds new files. If `python -c "import tkinter"` gives an error, install Tk with
`pacman -S mingw-w64-ucrt-x86_64-tk` (drop `ucrt-` for the MINGW64 shell). Start the IDE from the
MSYS2 shell so that it, the Run button and the Terminal tab all see MSYS2's PATH, which is how
they find QEMU and bash.

### Without installing

You can also use it without installing anything. In the source folder, double click
`lmc-ide.pyw` (or run `python lmc-ide.pyw`) for the IDE, and use `python lmcc-run.py prog.lmc`
for the compiler. Do not run the files inside `src/lmc/` directly, use one of the above.

    lmcc --examples          copies the example programs into ./lmc-examples
    lmcc lmc-examples/hello.lmc --run

If the IDE will not start, it prints the reason, and any crash is also written to
`.lmc_ide_error.log` in your home folder.

## The IDE

    lmc-ide [folder | file.lmc ...]

- Project folders. File, then Open folder (Ctrl+Shift+O) shows the folder as a tree on the left.
  Double click a file to open it. Right click for New file, New folder, Rename, Delete, and
  "Use as the main file". The IDE reopens your last folder next time. Ctrl+B hides the panel.
- The main file is `main.lmc` in the folder if there is one, otherwise the shallowest file that
  has a `main:` label, and it is marked "(main)" in the tree. Choose a different one with the
  right click menu and the IDE remembers it in a `.lmcproject` file in the folder.
- `IMP "drivers/uart"` is looked for next to the file doing the import, then from the project
  folder, so files in different subfolders can import each other by path. The `.lmc` ending is
  optional.
- Live syntax checking. Every problem in the program and in everything it imports is listed in
  the Problems tab, the bad lines are shaded with a red dot in the margin, and the status bar
  shows the message for the line your cursor is on. Double click a problem to jump to it.
  Checking uses what is on screen, so you do not need to save first.
- Tabs for several files. Files that nothing imports yet are checked on their own.
- F5 builds `kernel8.img` in the project folder (next to the main file if no folder is open).
  F6 builds and runs it in QEMU, with the serial output in the Console tab and a box for typing
  input to the program (`INP`). Shift+F6 stops it.
- A Terminal tab (Ctrl+` to open it). It starts in your project folder and runs whatever you type,
  so `lmcc`, `git`, `make` and your own scripts all work, and the output streams in as it
  arrives. Up and Down recall earlier commands, Stop or Ctrl+C ends a running command, and text
  you type while a program runs is sent to it. The folder carries over between commands, so
  `cd drivers && ls` leaves you in `drivers`, and the prompt shows where you are. Each command
  runs in its own shell process, so things that live inside one shell, such as `export`
  variables and aliases, do not carry over. The shell is bash if there is a real one on your
  PATH (such as MSYS2's), otherwise cmd on Windows or `$SHELL` elsewhere. Terminal, then
  "Choose shell..." picks another.
- F8 shows the machine code listing. Ctrl+F finds, Ctrl+G goes to a line.

The first time you press Run, the IDE asks where QEMU is if it is not on your PATH.

## The compiler

    lmcc prog.lmc                 build kernel8.img
    lmcc prog.lmc -o out.img      choose the output name
    lmcc prog.lmc -S prog.s       also write GNU assembly
    lmcc prog.lmc -I libs         also look for IMP files in the folder libs
    lmcc prog.lmc -l              print addresses, machine code and assembly
    lmcc prog.lmc --run           build and run in QEMU (Ctrl-A then X to quit)

Imports are searched for next to the importing file, then in `-I` folders, in the folders listed
in the `LMC_INCLUDE` environment variable, and in the project folder (the nearest folder above the
source that holds a `.lmcproject` file). The IDE's Terminal tab sets `LMC_INCLUDE` to your project
folder, so `lmcc` there finds imports the same way the IDE does.

`python -m lmc` runs the same compiler. It reports every error it finds, not just the first.

The compiler is also a library: `from lmc import compiler`, then `compiler.check(path, overrides)`
returns the errors for a program and `compiler.compile_file(path)` returns the assembly object,
the code bytes and the data bytes.

## Running on a Pi

In QEMU: `qemu-system-aarch64 -M raspi3b -kernel kernel8.img -display none -serial null -serial mon:stdio`

On a real Pi 3B, copy `kernel8.img` onto an SD card that holds the normal Pi firmware files and
put this in `config.txt`:

    arm_64bit=1
    enable_uart=1

Input and output go over the mini UART on GPIO 14 (TX) and GPIO 15 (RX) at 115200 baud, 8N1.
Use a USB serial cable and a terminal program.

## Language

The commands are those of Peter Higginson's LMC, plus `IMP`. One instruction per line. `;` or
`//` starts a comment. Instruction names are not case sensitive, labels are. A label can have a
colon or not, so `loop: LDA count`, `loop LDA count` and a `loop:` line on its own all work. The
program starts at the label `main`.

    HLT or COB     stop
    LDA x          accumulator = x
    STA x or STO x x = accumulator
    ADD x          accumulator = accumulator + x
    SUB x          accumulator = accumulator - x
    BRA label      branch always
    BRZ label      branch if the accumulator is zero
    BRP label      branch if the accumulator is zero or positive
    INP            read a decimal number from the serial port into the accumulator
    OUT            print the accumulator as a number, then a newline
    OTC            print the accumulator as a character
    DAT [value]    reserve a named memory location (default 0)
    IMP "file"     include another .lmc file once, relative to the importing file

The accumulator is a 64 bit signed integer and arithmetic wraps. There are no mailboxes and no
100 location limit. Your program and its named data sit in the Pi's memory as normal.

An operand `x` can be:

    name       a named location, declared with DAT (or INT, CHA and so on, below)
    0x200000   a real memory address, a multiple of 8. This is the address itself, not a mailbox number.
    #5         an immediate value, the number itself (decimal, 0x hex, or a character such as #'A'), not valid for STA

Addresses are checked: one that is not a multiple of 8 is an error, because the Pi faults on a
misaligned 8 byte access while the MMU is off. If you wrote `ADD 5` meaning the number 5, the
error suggests `ADD #5`.

Extras beyond Higginson's LMC:

    OUT x, OTC x   print a location or a CHA string directly, for example  OTC message
    INT [v]        4 byte signed integer
    CHA [v]        1 byte, a number, a character, or a "string"
    FLO [v]        4 byte float, stored only
    DUB [v]        8 byte float, stored only

`DAT` is 8 bytes. All data is aligned to its own size. Names starting with `__` are used by the
runtime. Imported files share one namespace, so a label can only be defined once across all files.

A program from Higginson's simulator runs as it is once its first instruction has the label
`main`. See `higginson.lmc` in the examples.

## Memory map

    0x80000    start of the image: startup code, your code, the runtime, then the data area
    0x80000    the stack grows down from here, below the image

Startup parks cores 1 to 3, sets the stack, sets up the UART and jumps to `main`.

## Tests

    python -m unittest discover tests

The tests check the compiler and its errors, and the IDE's project handling. The IDE tests need
a display and are skipped without one (on a headless Linux machine use `xvfb-run -a python -m
unittest discover tests`). If the GNU aarch64 binutils are installed the machine code is also
checked against the GNU assembler, and if QEMU is installed the hello example runs on an
emulated Pi.
