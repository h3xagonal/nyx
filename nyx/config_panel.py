"""
Panel presenting the configuration state for tor or nyx. Options can be edited
and the resulting configuration files saved.
"""

import curses
import threading

import nyx.controller
import nyx.popups

from nyx.util import panel, tor_config, tor_controller, ui_tools

from stem.control import State
from stem.util import conf, enum, str_tools

# attributes of a ConfigEntry

Field = enum.Enum(
  ('CATEGORY', 'Category'),
  ('OPTION', 'Option Name'),
  ('VALUE', 'Value'),
  ('TYPE', 'Arg Type'),
  ('ARG_USAGE', 'Arg Usage'),
  ('SUMMARY', 'Summary'),
  ('DESCRIPTION', 'Description'),
  ('MAN_ENTRY', 'Man Page Entry'),
  ('IS_DEFAULT', 'Is Default'),
)


def conf_handler(key, value):
  if key == 'features.config.selectionDetails.height':
    return max(0, value)
  elif key == 'features.config.state.colWidth.option':
    return max(5, value)
  elif key == 'features.config.state.colWidth.value':
    return max(5, value)
  elif key == 'features.config.order':
    return conf.parse_enum_csv(key, value[0], Field, 3)


CONFIG = conf.config_dict('nyx', {
  'attr.config.category_color': {},
  'attr.config.field_color': {},
  'features.config.order': [Field.MAN_ENTRY, Field.OPTION, Field.IS_DEFAULT],
  'features.config.selectionDetails.height': 6,
  'features.config.prepopulateEditValues': True,
  'features.config.state.showPrivateOptions': False,
  'features.config.state.showVirtualOptions': False,
  'features.config.state.colWidth.option': 25,
  'features.config.state.colWidth.value': 15,
}, conf_handler)


class ConfigEntry():
  """
  Configuration option in the panel.
  """

  def __init__(self, option, type, is_default):
    man_entry = tor_config.get_config_description(option)

    self.fields = {
      Field.OPTION: option,
      Field.TYPE: type,
      Field.IS_DEFAULT: is_default,
      Field.SUMMARY: tor_config.get_config_summary(option),

      Field.MAN_ENTRY: man_entry.index if man_entry else 99999,  # sorts non-man entries last
      Field.CATEGORY: man_entry.category if man_entry else tor_config.Category.UNKNOWN,
      Field.ARG_USAGE: man_entry.arg_usage if man_entry else '',
      Field.DESCRIPTION: man_entry.description if man_entry else '',
    }

    # uses the full man page description if a summary is unavailable

    if self.fields[Field.SUMMARY] is None:
      self.fields[Field.SUMMARY] = self.fields[Field.DESCRIPTION]

    # cache of what's displayed for this configuration option

    self.label_cache = None
    self.label_cache_args = None

  def get(self, field):
    """
    Provides back the value in the given field.

    Arguments:
      field - enum for the field to be provided back
    """

    return self._get_value() if field == Field.VALUE else self.fields[field]

  def get_all(self, fields):
    """
    Provides back a list with the given field values.

    Arguments:
      field - enums for the fields to be provided back
    """

    return [self.get(field) for field in fields]

  def get_label(self, option_width, value_width, summary_width):
    """
    Provides display string of the configuration entry with the given
    constraints on the width of the contents.

    Arguments:
      option_width  - width of the option column
      value_width   - width of the value column
      summary_width - width of the summary column
    """

    # Fetching the display entries is very common so this caches the values.
    # Doing this substantially drops cpu usage when scrolling (by around 40%).

    arg_set = (option_width, value_width, summary_width)

    if not self.label_cache or self.label_cache_args != arg_set:
      option_label = str_tools.crop(self.get(Field.OPTION), option_width)
      value_label = str_tools.crop(self.get(Field.VALUE), value_width)
      summary_label = str_tools.crop(self.get(Field.SUMMARY), summary_width, None)
      line_text_layout = '%%-%is %%-%is %%-%is' % (option_width, value_width, summary_width)
      self.label_cache = line_text_layout % (option_label, value_label, summary_label)
      self.label_cache_args = arg_set

    return self.label_cache

  def is_unset(self):
    """
    True if we have no value, false otherwise.
    """

    conf_value = tor_controller().get_conf(self.get(Field.OPTION), [], True)

    return not bool(conf_value)

  def _get_value(self):
    """
    Provides the current value of the configuration entry, taking advantage of
    the tor_tools caching to effectively query the accurate value. This uses the
    value's type to provide a user friendly representation if able.
    """

    conf_value = ', '.join(tor_controller().get_conf(self.get(Field.OPTION), [], True))

    # provides nicer values for recognized types

    if not conf_value:
      conf_value = '<none>'
    elif self.get(Field.TYPE) == 'Boolean' and conf_value in ('0', '1'):
      conf_value = 'False' if conf_value == '0' else 'True'
    elif self.get(Field.TYPE) == 'DataSize' and conf_value.isdigit():
      conf_value = str_tools.size_label(int(conf_value))
    elif self.get(Field.TYPE) == 'TimeInterval' and conf_value.isdigit():
      conf_value = str_tools.time_label(int(conf_value), is_long = True)

    return conf_value


class ConfigPanel(panel.Panel):
  """
  Renders a listing of the tor or nyx configuration state, allowing options to
  be selected and edited.
  """

  def __init__(self, stdscr):
    panel.Panel.__init__(self, stdscr, 'configuration', 0)

    self.conf_contents = []
    self.conf_important_contents = []
    self.scroller = ui_tools.Scroller(True)
    self._vals_lock = threading.RLock()

    # shows all configuration options if true, otherwise only the ones with
    # the 'important' flag are shown

    self.show_all = False

    tor_controller().add_status_listener(self.reset_listener)
    self._load_config_options()

  def reset_listener(self, controller, event_type, _):
    # fetches configuration options if a new instance, otherewise keeps our
    # current contents

    if event_type == State.INIT:
      self._load_config_options()

  def _load_config_options(self):
    """
    Fetches the configuration options available from tor or nyx.
    """

    self.conf_contents = []
    self.conf_important_contents = []

    custom_options = tor_config.get_custom_options()
    config_names = tor_controller().get_info('config/names', None)

    if config_names:
      config_option_lines = config_names.strip().split('\n')

      for line in config_option_lines:
        # lines are of the form "<option> <type>[ <documentation>]", like:
        # UseEntryGuards Boolean
        # documentation is aparently only in older versions (for instance,
        # 0.2.1.25)

        line_comp = line.strip().split(' ')
        conf_option, conf_type = line_comp[0], line_comp[1]

        # skips private and virtual entries if not configured to show them

        if not CONFIG['features.config.state.showPrivateOptions'] and conf_option.startswith('__'):
          continue
        elif not CONFIG['features.config.state.showVirtualOptions'] and conf_type == 'Virtual':
          continue

        self.conf_contents.append(ConfigEntry(conf_option, conf_type, conf_option not in custom_options))

    # mirror listing with only the important configuration options

    self.conf_important_contents = []

    for entry in self.conf_contents:
      if tor_config.is_important(entry.get(Field.OPTION)):
        self.conf_important_contents.append(entry)

    # if there aren't any important options then show everything

    if not self.conf_important_contents:
      self.conf_important_contents = self.conf_contents

    self.set_sort_order()  # initial sorting of the contents

  def get_selection(self):
    """
    Provides the currently selected entry.
    """

    return self.scroller.get_cursor_selection(self._get_config_options())

  def set_filtering(self, is_filtered):
    """
    Sets if configuration options are filtered or not.

    Arguments:
      is_filtered - if true then only relatively important options will be
                   shown, otherwise everything is shown
    """

    self.show_all = not is_filtered

  def set_sort_order(self, ordering = None):
    """
    Sets the configuration attributes we're sorting by and resorts the
    contents.

    Arguments:
      ordering - new ordering, if undefined then this resorts with the last
                 set ordering
    """

    with self._vals_lock:
      if ordering:
        CONFIG['features.config.order'] = ordering

      self.conf_contents.sort(key=lambda i: (i.get_all(CONFIG['features.config.order'])))
      self.conf_important_contents.sort(key=lambda i: (i.get_all(CONFIG['features.config.order'])))

  def show_sort_dialog(self):
    """
    Provides the sort dialog for our configuration options.
    """

    # set ordering for config options

    title_label = 'Config Option Ordering:'
    old_selection = CONFIG['features.config.order']
    option_colors = dict([(field, CONFIG['attr.config.field_color'].get(field, 'white')) for field in Field])
    results = nyx.popups.show_sort_dialog(title_label, Field, old_selection, option_colors)

    if results:
      self.set_sort_order(results)

  def handle_key(self, key):
    with self._vals_lock:
      if key.is_scroll():
        page_height = self.get_preferred_size()[0] - 1
        detail_panel_height = CONFIG['features.config.selectionDetails.height']

        if detail_panel_height > 0 and detail_panel_height + 2 <= page_height:
          page_height -= (detail_panel_height + 1)

        is_changed = self.scroller.handle_key(key, self._get_config_options(), page_height)

        if is_changed:
          self.redraw(True)
      elif key.is_selection() and self._get_config_options():
        # Prompts the user to edit the selected configuration value. The
        # interface is locked to prevent updates between setting the value
        # and showing any errors.

        with panel.CURSES_LOCK:
          selection = self.get_selection()
          config_option = selection.get(Field.OPTION)

          initial_value = '' if selection.is_unset() else selection.get(Field.VALUE)
          prompt_msg = '%s Value (esc to cancel): ' % config_option
          is_prepopulated = CONFIG['features.config.prepopulateEditValues']
          new_value = nyx.popups.input_prompt(prompt_msg, initial_value if is_prepopulated else '')

          if new_value is not None and new_value != initial_value:
            try:
              if selection.get(Field.TYPE) == 'Boolean':
                # if the value's a boolean then allow for 'true' and 'false' inputs

                if new_value.lower() == 'true':
                  new_value = '1'
                elif new_value.lower() == 'false':
                  new_value = '0'
              elif selection.get(Field.TYPE) == 'LineList':
                # set_option accepts list inputs when there's multiple values
                new_value = new_value.split(',')

              tor_controller().set_conf(config_option, new_value)

              # forces the label to be remade with the new value

              selection.label_cache = None

              # resets the is_default flag

              custom_options = tor_config.get_custom_options()
              selection.fields[Field.IS_DEFAULT] = config_option not in custom_options

              self.redraw(True)
            except Exception as exc:
              nyx.popups.show_msg('%s (press any key)' % exc)
      elif key.match('a'):
        self.show_all = not self.show_all
        self.redraw(True)
      elif key.match('s'):
        self.show_sort_dialog()
      elif key.match('v'):
        self.show_write_dialog()
      else:
        return False

      return True

  def show_write_dialog(self):
    """
    Provies an interface to confirm if the configuration is saved and, if so,
    where.
    """

    # display a popup for saving the current configuration

    config_lines = tor_config.get_custom_options(True)
    popup, width, height = nyx.popups.init(len(config_lines) + 2)

    if not popup:
      return

    try:
      # displayed options (truncating the labels if there's limited room)

      if width >= 30:
        selection_options = ('Save', 'Save As...', 'Cancel')
      else:
        selection_options = ('Save', 'Save As', 'X')

      # checks if we can show options beside the last line of visible content

      is_option_line_separate = False
      last_index = min(height - 2, len(config_lines) - 1)

      # if we don't have room to display the selection options and room to
      # grow then display the selection options on its own line

      if width < (30 + len(config_lines[last_index])):
        popup.set_height(height + 1)
        popup.redraw(True)  # recreates the window instance
        new_height, _ = popup.get_preferred_size()

        if new_height > height:
          height = new_height
          is_option_line_separate = True

      selection = 2

      while True:
        # if the popup has been resized then recreate it (needed for the
        # proper border height)

        new_height, new_width = popup.get_preferred_size()

        if (height, width) != (new_height, new_width):
          height, width = new_height, new_width
          popup.redraw(True)

        # if there isn't room to display the popup then cancel it

        if height <= 2:
          selection = 2
          break

        popup.win.erase()
        popup.win.box()
        popup.addstr(0, 0, 'Configuration being saved:', curses.A_STANDOUT)

        visible_config_lines = height - 3 if is_option_line_separate else height - 2

        for i in range(visible_config_lines):
          line = str_tools.crop(config_lines[i], width - 2)

          if ' ' in line:
            option, arg = line.split(' ', 1)
            popup.addstr(i + 1, 1, option, curses.A_BOLD, 'green')
            popup.addstr(i + 1, len(option) + 2, arg, curses.A_BOLD, 'cyan')
          else:
            popup.addstr(i + 1, 1, line, curses.A_BOLD, 'green')

        # draws selection options (drawn right to left)

        draw_x = width - 1

        for i in range(len(selection_options) - 1, -1, -1):
          option_label = selection_options[i]
          draw_x -= (len(option_label) + 2)

          # if we've run out of room then drop the option (this will only
          # occure on tiny displays)

          if draw_x < 1:
            break

          selection_format = curses.A_STANDOUT if i == selection else curses.A_NORMAL
          x = popup.addstr(height - 2, draw_x, '[')
          x = popup.addstr(height - 2, x, option_label, selection_format, curses.A_BOLD)
          popup.addstr(height - 2, x, ']')

          draw_x -= 1  # space gap between the options

        popup.win.refresh()

        key = nyx.controller.get_controller().key_input()

        if key.match('left'):
          selection = max(0, selection - 1)
        elif key.match('right'):
          selection = min(len(selection_options) - 1, selection + 1)
        elif key.is_selection():
          break

      if selection in (0, 1):
        loaded_torrc, prompt_canceled = tor_config.get_torrc(), False

        try:
          config_location = loaded_torrc.get_config_location()
        except IOError:
          config_location = ''

        if selection == 1:
          # prompts user for a configuration location
          config_location = nyx.popups.input_prompt('Save to (esc to cancel): ', config_location)

          if not config_location:
            prompt_canceled = True

        if not prompt_canceled:
          try:
            tor_config.save_conf(config_location, config_lines)
            msg = 'Saved configuration to %s' % config_location
          except IOError as exc:
            msg = 'Unable to save configuration (%s)' % exc.strerror

          nyx.popups.show_msg(msg, 2)
    finally:
      nyx.popups.finalize()

  def get_help(self):
    return [
      ('up arrow', 'scroll up a line', None),
      ('down arrow', 'scroll down a line', None),
      ('page up', 'scroll up a page', None),
      ('page down', 'scroll down a page', None),
      ('enter', 'edit configuration option', None),
      ('v', 'save configuration', None),
      ('a', 'toggle option filtering', None),
      ('s', 'sort ordering', None),
    ]

  def draw(self, width, height):
    with self._vals_lock:
      # panel with details for the current selection

      detail_panel_height = CONFIG['features.config.selectionDetails.height']
      is_scrollbar_visible = False

      if detail_panel_height == 0 or detail_panel_height + 2 >= height:
        # no detail panel

        detail_panel_height = 0
        scroll_location = self.scroller.get_scroll_location(self._get_config_options(), height - 1)
        cursor_selection = self.get_selection()
        is_scrollbar_visible = len(self._get_config_options()) > height - 1
      else:
        # Shrink detail panel if there isn't sufficient room for the whole
        # thing. The extra line is for the bottom border.

        detail_panel_height = min(height - 1, detail_panel_height + 1)
        scroll_location = self.scroller.get_scroll_location(self._get_config_options(), height - 1 - detail_panel_height)
        cursor_selection = self.get_selection()
        is_scrollbar_visible = len(self._get_config_options()) > height - detail_panel_height - 1

        if cursor_selection is not None:
          self._draw_selection_panel(cursor_selection, width, detail_panel_height, is_scrollbar_visible)

      # draws the top label

      if self.is_title_visible():
        hidden_msg = "press 'a' to hide most options" if self.show_all else "press 'a' to show all options"
        title_label = 'Tor Configuration (%s):' % hidden_msg
        self.addstr(0, 0, title_label, curses.A_STANDOUT)

      # draws left-hand scroll bar if content's longer than the height

      scroll_offset = 1

      if is_scrollbar_visible:
        scroll_offset = 3
        self.add_scroll_bar(scroll_location, scroll_location + height - detail_panel_height - 1, len(self._get_config_options()), 1 + detail_panel_height)

      option_width = CONFIG['features.config.state.colWidth.option']
      value_width = CONFIG['features.config.state.colWidth.value']
      description_width = max(0, width - scroll_offset - option_width - value_width - 2)

      # if the description column is overly long then use its space for the
      # value instead

      if description_width > 80:
        value_width += description_width - 80
        description_width = 80

      for line_number in range(scroll_location, len(self._get_config_options())):
        entry = self._get_config_options()[line_number]
        draw_line = line_number + detail_panel_height + 1 - scroll_location

        line_format = [curses.A_NORMAL if entry.get(Field.IS_DEFAULT) else curses.A_BOLD]

        if entry.get(Field.CATEGORY):
          line_format += [CONFIG['attr.config.category_color'].get(entry.get(Field.CATEGORY), 'white')]

        if entry == cursor_selection:
          line_format += [curses.A_STANDOUT]

        line_text = entry.get_label(option_width, value_width, description_width)
        self.addstr(draw_line, scroll_offset, line_text, *line_format)

        if draw_line >= height:
          break

  def _get_config_options(self):
    return self.conf_contents if self.show_all else self.conf_important_contents

  def _draw_selection_panel(self, selection, width, detail_panel_height, is_scrollbar_visible):
    """
    Renders a panel for the selected configuration option.
    """

    # This is a solid border unless the scrollbar is visible, in which case a
    # 'T' pipe connects the border to the bar.

    ui_tools.draw_box(self, 0, 0, width, detail_panel_height + 1)

    if is_scrollbar_visible:
      self.addch(detail_panel_height, 1, curses.ACS_TTEE)

    selection_format = (curses.A_BOLD, CONFIG['attr.config.category_color'].get(selection.get(Field.CATEGORY), 'white'))

    # first entry:
    # <option> (<category> Option)

    option_label = ' (%s Option)' % selection.get(Field.CATEGORY)
    self.addstr(1, 2, selection.get(Field.OPTION) + option_label, *selection_format)

    # second entry:
    # Value: <value> ([default|custom], <type>, usage: <argument usage>)

    if detail_panel_height >= 3:
      value_attr_label = ', '.join([
        'default' if selection.get(Field.IS_DEFAULT) else 'custom',
        selection.get(Field.TYPE),
        'usage: %s' % (selection.get(Field.ARG_USAGE))
      ])

      value_label_width = width - 12 - len(value_attr_label)
      value_label = str_tools.crop(selection.get(Field.VALUE), value_label_width)

      self.addstr(2, 2, 'Value: %s (%s)' % (value_label, value_attr_label), *selection_format)

    # remainder is filled with the man page description

    description_height = max(0, detail_panel_height - 3)
    description_content = 'Description: ' + selection.get(Field.DESCRIPTION)

    for i in range(description_height):
      # checks if we're done writing the description

      if not description_content:
        break

      # there's a leading indent after the first line

      if i > 0:
        description_content = '  ' + description_content

      # we only want to work with content up until the next newline

      if '\n' in description_content:
        line_content, description_content = description_content.split('\n', 1)
      else:
        line_content, description_content = description_content, ''

      if i != description_height - 1:
        # there's more lines to display

        msg, remainder = str_tools.crop(line_content, width - 3, 4, 4, str_tools.Ending.HYPHEN, True)
        description_content = remainder.strip() + description_content
      else:
        # this is the last line, end it with an ellipse

        msg = str_tools.crop(line_content, width - 3, 4, 4)

      self.addstr(3 + i, 2, msg, *selection_format)
