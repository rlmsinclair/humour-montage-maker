
import os
import sys

# Add common binary paths to PATH
paths_to_add = [
    '/usr/local/bin',
    '/usr/bin',
    '/bin',
    '/opt/homebrew/bin',
    '/usr/sbin'
]

if 'PATH' not in os.environ:
    os.environ['PATH'] = ''

os.environ['PATH'] = ':'.join(paths_to_add) + ':' + os.environ['PATH']
