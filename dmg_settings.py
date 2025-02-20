# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import os.path

# Use custom volume icon
#icon = os.path.join(application, 'Contents', 'Resources', 'Icon.icns')

# Volume format (see hdiutil create -help)
format = 'UDRW'

# Volume size (must be large enough for all files)
size = '500M'

# Files to include
files = ['dist/Udder AI.app']

# Symlinks to create
symlinks = {'Applications': '/Applications'}

# Window configuration
window_rect = ((100, 100), (640, 400))
default_view = 'icon-view'

# Background
background = 'builtin-arrow'

# Icon view configuration
arrange_by = None
grid_offset = (0, 0)
grid_spacing = 120
scroll_position = (0, 0)
label_pos = 'bottom'
text_size = 14
icon_size = 96

# Icon locations
icon_locations = {
    'Udder AI.app': (160, 170),
    'Applications': (480, 170)
}

# Window settings
show_status_bar = False
show_tab_view = False
show_toolbar = False
show_pathbar = False
show_sidebar = False

# Additional settings
format = 'UDZO'
compression_level = 9
