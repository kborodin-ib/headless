#!/usr/bin/env python3
"""
Linux Terminal Configuration Script
Applies .bashrc, .tmux.conf, and .vimrc settings with Parrot OS styled CLI and below colorcheme:
Colors: pale purple (183), pale green (157), brown (130)
"""

import os
import sys
from pathlib import Path


def append_to_file(filepath, content, marker):
    """
    Append content to file if marker doesn't exist.
    Create file if it doesn't exist.
    """
    filepath = Path(filepath).expanduser()
    
    # Create file if it doesn't exist
    if not filepath.exists():
        filepath.touch()
        print(f"  → Created new file: {filepath}")
    
    # Check if content already exists
    try:
        with open(filepath, 'r') as f:
            existing_content = f.read()
        
        if marker in existing_content:
            print(f"  → {filepath.name} already configured (skipping)")
            return False
    except Exception as e:
        print(f"  ✗ Error reading {filepath}: {e}")
        return False
    
    # Append new content
    try:
        with open(filepath, 'a') as f:
            f.write("\n" + content + "\n")
        print(f"  ✓ Appended configuration to {filepath}")
        return True
    except Exception as e:
        print(f"  ✗ Error writing to {filepath}: {e}")
        return False


def setup_bashrc():
    """Create/append ~/.bashrc with custom prompt and colors"""
    print("\n🔧 Configuring .bashrc...")
    
    bashrc_content = '''# ========== PARROT OS TERMINAL CONFIG ==========
# Parrot OS style prompt with crab emoji and pwd
PALE_PURPLE='\\[\\033[38;5;183m\\]'
PALE_GREEN='\\[\\033[38;5;157m\\]'
BROWN='\\[\\033[38;5;130m\\]'
RESET='\\[\\033[0m\\]'

PS1="${PALE_PURPLE}┌─${PALE_GREEN}[${PALE_PURPLE}🦀${PALE_GREEN}]${RESET} ${BROWN}\\$(pwd)${RESET}\\n${PALE_PURPLE}└─${PALE_GREEN}\\$${RESET} "

# Aliases and colors
alias ls='ls --color=auto'
alias grep='grep --color=auto'
alias diff='diff --color=auto'

# LS_COLORS matching color scheme
export LS_COLORS='di=38;5;183:fi=38;5;157:ln=38;5;130:ex=38;5;157;1:*.sh=38;5;157;1:*.py=38;5;157;1:*.c=38;5;157;1:*.cpp=38;5;157;1:*.go=38;5;157;1:*.rs=38;5;157;1:*.js=38;5;157;1:*.ts=38;5;157;1:*.java=38;5;157;1:*.rb=38;5;157;1:*.md=38;5;130:*.txt=38;5;130:*.conf=38;5;183:*.yaml=38;5;183:*.yml=38;5;183:*.json=38;5;183:*.xml=38;5;183:*.sql=38;5;183:*.tar=38;5;183:*.gz=38;5;183:*.zip=38;5;183:*.7z=38;5;183:*.jpg=38;5;130:*.jpeg=38;5;130:*.png=38;5;130:*.gif=38;5;130:*.pdf=38;5;130:*.mp3=38;5;130:*.mp4=38;5;130'
# ================================================'''
    
    return append_to_file(Path.home() / '.bashrc', bashrc_content, 'PARROT OS TERMINAL CONFIG')


def setup_tmux_conf():
    """Create/append ~/.tmux.conf with custom colors"""
    print("\n🔧 Configuring .tmux.conf...")
    
    tmux_content = '''# ========== PARROT OS TMUX CONFIG ==========
# Terminal colors
set -g default-terminal "screen-256color"
set -ga terminal-overrides ",xterm-256color:RGB"

# Status bar
set -g status-style "bg=colour16,fg=colour157"

# Active window (pale purple)
set -g window-status-current-style "bg=colour183,fg=colour16,bold"
set -g window-status-current-format " #I:#W "

# Inactive window (pale green)
set -g window-status-style "bg=colour16,fg=colour157"
set -g window-status-format " #I:#W "

# Pane borders
set -g pane-border-style "fg=colour130"
set -g pane-active-border-style "fg=colour157"

# Messages (pale purple background)
set -g message-style "bg=colour183,fg=colour16"
set -g message-command-style "bg=colour183,fg=colour16"

# Status line
set -g status-left "#[bg=colour183,fg=colour16] #S #[default] "
set -g status-right "#[bg=colour130,fg=colour16] %H:%M #[default]"

# Mouse and vim bindings
set -g mouse on
setw -g mode-keys vi
# ============================================'''
    
    return append_to_file(Path.home() / '.tmux.conf', tmux_content, 'PARROT OS TMUX CONFIG')


def setup_vimrc():
    """Create/append ~/.vimrc with custom color scheme"""
    print("\n🔧 Configuring .vimrc...")
    
    vimrc_content = '''
" ========== PARROT OS VIM CONFIG ==========
syntax on
set termguicolors
set background=dark
set number
set cursorline
set cursorcolumn
set ruler

" Color scheme - pale purple, pale green, brown
highlight Normal ctermfg=157 ctermbg=16
highlight Comment ctermfg=130 cterm=italic
highlight String ctermfg=157
highlight Number ctermfg=183
highlight Keyword ctermfg=183 cterm=bold
highlight Function ctermfg=157
highlight Type ctermfg=183
highlight Statement ctermfg=183 cterm=bold
highlight Identifier ctermfg=157
highlight PreProc ctermfg=183
highlight Special ctermfg=130

highlight LineNr ctermfg=130 ctermbg=16
highlight CursorLineNr ctermfg=183 ctermbg=16 cterm=bold
highlight StatusLine ctermfg=16 ctermbg=183 cterm=bold
highlight StatusLineNC ctermfg=157 ctermbg=16
highlight Search ctermfg=16 ctermbg=157
highlight IncSearch ctermfg=16 ctermbg=183 cterm=bold
highlight Visual ctermfg=16 ctermbg=157
highlight Error ctermfg=16 ctermbg=130
highlight Cursor ctermfg=16 ctermbg=157
highlight MatchParen ctermfg=16 ctermbg=183 cterm=bold
" =========================================='''
    
    return append_to_file(Path.home() / '.vimrc', vimrc_content, 'PARROT OS VIM CONFIG')


def print_summary(results):
    """Print configuration summary"""
    print("\n" + "="*70)
    print("🦀 Terminal Configuration Applied Successfully!")
    print("="*70)
    print("\nColor Scheme:")
    print("  • Pale Purple (183): Directories, Keywords, Numbers")
    print("  • Pale Green (157):  Files, Strings, Functions")
    print("  • Brown (130):       Links, Comments, Archives")
    print("\nFiles Status:")
    print(f"  • ~/.bashrc     {'[UPDATED]' if results[0] else '[SKIPPED/EXISTS]'}")
    print(f"  • ~/.tmux.conf  {'[UPDATED]' if results[1] else '[SKIPPED/EXISTS]'}")
    print(f"  • ~/.vimrc      {'[UPDATED]' if results[2] else '[SKIPPED/EXISTS]'}")
    print("\nNext Steps:")
    print("  1. Reload bash:       source ~/.bashrc")
    print("  2. Reload tmux:       tmux source-file ~/.tmux.conf")
    print("  3. Restart terminal:  bash")
    print("="*70 + "\n")


def main():
    """Main function"""
    print("\n" + "="*70)
    print("🦀 Parrot OS Terminal Configuration Script")
    print("="*70)
    
    try:
        results = []
        
        results.append(setup_bashrc())
        results.append(setup_tmux_conf())
        results.append(setup_vimrc())
        
        print_summary(results)
        
        if any(results):
            print("✓ Configuration complete! Run 'source ~/.bashrc' to reload.")
        else:
            print("ℹ All files already configured.")
        
        sys.exit(0)
        
    except Exception as e:
        print(f"\n✗ Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
