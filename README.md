# herdr-icucomefrom

A [herdr](https://herdr.dev) plugin that guesses whether the herdr client
you're using came in over ssh or runs on the local machine. Use it to color
your prompt, or to decide in a script whether opening a browser makes sense.

Panes keep the environment the herdr server started with, so after you attach
over ssh every pane still looks local. `herdr-icucomefrom` looks at the clients
attached to the server instead.

## Install

```sh
herdr plugin install chmouel/herdr-icucomefrom
```

Install symlinks `herdr-icucomefrom` into `~/.local/bin` when that directory
exists, and leaves alone any file or symlink of your own with that name. If
you keep commands somewhere else, link `bin/herdr-icucomefrom` from the plugin
directory yourself (`herdr plugin list` shows where it lives).

From a checkout, `herdr plugin link ~/path/to/herdr-icucomefrom` works too.
herdr creates the `~/.local/bin` link at the next server start, or you can run
`sh install.sh` to get it now.

## Usage

```text
herdr-icucomefrom            print ssh or local
herdr-icucomefrom is-ssh     exit 0 on ssh, 1 otherwise
herdr-icucomefrom is-local   exit 0 on local, 1 otherwise
herdr-icucomefrom status     show every client and why the guess came out that way
```

It always answers. Outside herdr, or when detection fails, it uses the
calling shell's own `SSH_CONNECTION` and `SSH_TTY` and prints the reason on
stderr.

Prompt color in zsh:

```zsh
autoload -Uz add-zsh-hook
_herdr_color() { [[ $(herdr-icucomefrom 2>/dev/null) == ssh ]] && psvar[1]=yellow || psvar[1]=green }
add-zsh-hook precmd _herdr_color
PROMPT='%F{${psvar[1]}}%~%f %# '
```

In bash:

```bash
_herdr_color() {
  local c=32
  [[ $(herdr-icucomefrom 2>/dev/null) == ssh ]] && c=33
  PS1="\[\e[${c}m\]\w\[\e[0m\] \$ "
}
PROMPT_COMMAND="_herdr_color${PROMPT_COMMAND:+;$PROMPT_COMMAND}"
```

In a script:

```sh
herdr-icucomefrom is-local && xdg-open "$url"
```

Each call takes around 70 ms on Linux, most of it in `ss`.

The plugin also adds a "Show client transport" action that opens `status` in
a popup.

## How it guesses

Every process connected to the server's `herdr-client.sock` is a client. A
client counts as ssh when sshd, sshd-session or mosh-server is among its
parents, or when its environment has `SSH_CONNECTION` or `SSH_TTY`. That
covers `herdr --remote` too.

If all clients agree, that's the answer. Otherwise the terminal you typed in
during the last 10 seconds wins, which is the one you just pressed Enter in
when your prompt draws. With no recent typing and a `herdr --remote` bridge
attached, the answer is ssh, since typing through a bridge leaves no trace on
the local terminals. Failing that, the terminal with the latest input wins.

## Limitations

- herdr knows which client is in the foreground but doesn't expose it, so
  with several clients attached the answer is a guess.
- Linux records terminal input at most every 8 seconds, so switching between
  two terminals within a few seconds can give the wrong answer for a moment.
- The tool can't see typing through a `herdr --remote` bridge, so a local
  terminal you touched in the last 10 seconds wins over it.
- macOS support is untested.

## Requirements

Python 3.9 or later, plus `ss` from iproute2 on Linux or `lsof` on macOS.

## License

MIT
