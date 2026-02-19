# cl/clp completion for zsh
# Source this file AFTER compinit in .zshrc
# Install to: ~/.claude/cl-completion.zsh

_cl_complete() {
  local -a opts
  opts=(
    '-n:Clean start - no task picker'
    '--clean:Clean start'
    '-t:Show task picker'
    '--tasks:Task picker'
    '-r:Resume session'
    '-c:Continue last session'
    '-h:Show help'
    '--help:Help'
  )
  _describe 'cl options' opts
}

compdef _cl_complete cl clp
