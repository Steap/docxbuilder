# -*- coding: utf-8 -*-
"""
    sphinx-docxwriter
    ~~~~~~~~~~~~~~~~~~~~~~~~~~

    Modified custom docutils writer for OpenXML (docx).
    Original code from 'sphinxcontrib-documentwriter'

    :copyright:
        Copyright 2011 by haraisao at gmail dot com 
    :license: MIT, see LICENSE for details.
"""
"""
    sphinxcontrib-docxwriter
    ~~~~~~~~~~~~~~~~~~~~~~~~~~

    Custom docutils writer for OpenXML (docx).

    :copyright:
        Copyright 2010 by shimizukawa at gmail dot com (Sphinx-users.jp).
    :license: BSD, see LICENSE for details.
"""

import hashlib
import os
import re
import sys

from docutils import nodes, writers
from lxml import etree
from sphinx import addnodes
from sphinx.environment.adapters.toctree import TocTree
from sphinx.ext import graphviz
from sphinx.locale import admonitionlabels, _
from sphinx.util import logging

from docxbuilder import docx
from docxbuilder.highlight import DocxPygmentsBridge

#
# Is the PIL imaging library installed?
try:
    from PIL import Image
except ImportError as exp:
    Image = None

# Utility functions

def get_image_size(filename):
    if Image is None:
        raise RuntimeError(
            'image size not fully specified and PIL not installed')
    with Image.open(filename, 'r') as imageobj:
        dpi = imageobj.info.get('dpi', (72, 72))
        # dpi information can be (xdpi, ydpi) or xydpi
        try:
            iter(dpi)
        except:
            dpi = (dpi, dpi)
        width = imageobj.size[0]
        height = imageobj.size[1]
        cmperin = 2.54
        return (width * cmperin / dpi[0], height * cmperin / dpi[1])

def convert_to_twip_size(size_with_unit, max_width):
    if size_with_unit is None:
        return None
    if size_with_unit.endswith('%'):
        return max_width * float(size_with_unit[:-1]) / 100

    match = re.match(r'^(\d+(?:\.\d*)?)(\D*)$', size_with_unit)
    if not match:
        raise RuntimeError('Unexpected length unit: %s' % size_with_unit)
    size = float(match.group(1))
    unit = match.group(2)
    if not unit:
        unit = 'px'

    twipperin = 1440.0
    cmperin = 2.54
    twippercm = twipperin / cmperin
    ratio_map = {
            'em': 12 * twipperin / 144, # TODO: Use Body Text font size
            'ex': 12 * twipperin / 144,
            'mm': twippercm / 10, 'cm': twippercm, 'in': twipperin,
            'px': twipperin / 96, 'pt': twipperin / 72, 'pc': twipperin / 6,
    }
    ratio = ratio_map.get(unit)
    if ratio is None:
        raise RuntimeError('Unknown length unit: %s' % size_with_unit)
    return size * ratio

def convert_to_cm_size(twip_size):
    if twip_size is None:
        return None
    twipperin = 1440.0
    cmperin = 2.54
    return twip_size / twipperin * cmperin

def adjust_size(max_size, size, other_size):
    if size > max_size:
        ratio = max_size / size
        return max_size, other_size * ratio
    return size, other_size

def has_caption(image_node):
    parent = image_node.parent
    if not isinstance(parent, nodes.figure):
        return False
    index = parent.index(image_node)
    caption_index = parent.first_child_matching_class(nodes.caption, index + 1)
    return caption_index is not None

def make_bookmark_name(docname, id):
    # The pattern Office enables to handle as a bookmark is ^(?!\d)\w{1,40}$
    hash = hashlib.md5(('%s/%s' % (docname, id)).encode('utf8'))
    return '_' + hash.hexdigest()

def count_colspec(table_node):
    tgroup = next(
            (c for c in table_node.children if isinstance(c, nodes.tgroup)),
            None)
    if tgroup is None:
        return 0
    return sum((1 for c in tgroup.children if isinstance(c, nodes.colspec)))

#
#  DocxWriter class for sphinx
#


class DocxWriter(writers.Writer):
    supported = ('docx',)
    settings_spec = ('No options here.', '', ())
    settings_defaults = {}

    output = None

    def __init__(self, builder):
        writers.Writer.__init__(self)
        self.builder = builder

        self._title = ''
        self._author = ''
        self._props = {}

    def set_doc_properties(self, title, author, props):
        self._title = title
        self._author = author
        self._props = props

    def translate(self):
        visitor = self.builder.create_translator(self.document, self.builder)
        self.document.walkabout(visitor)
        self.output = visitor.asbytes()

#
#  DocxTranslator class for sphinx
#

def to_error_string(contents):
    from xml.etree.ElementTree import tostring
    func = lambda xml: tostring(xml, encoding='utf8').decode('utf8')
    return type(contents).__name__ + '\n' + func(contents.to_xml())

class BookmarkElement(object):
    pass

class ParagraphElement(object):
    pass

class TableElement(object):
    pass

class SdtElement(object):
    pass

class BookmarkStart(BookmarkElement):
    def __init__(self, id, name):
        self._id = id
        self._name = name

    def to_xml(self):
        return docx.make_bookmark_start(self._id, self._name)

class BookmarkEnd(BookmarkElement):
    def __init__(self, id):
        self._id = id

    def to_xml(self):
        return docx.make_bookmark_end(self._id)

class Paragraph(ParagraphElement):
    default_style_id = None

    def __init__(self, indent=None, right_indent=None,
                 paragraph_style=None, align=None,
                 keep_lines=False, keep_next=False,
                 list_info=None, preserve_space=False):
        self._contents_stack = [[]]
        self._text_style_stack = []
        self._preserve_space = preserve_space
        self._indent = indent
        self._right_indent = right_indent
        self._style = paragraph_style
        self._align = align
        self._keep_lines = keep_lines
        self._keep_next = keep_next
        self._list_info = list_info

    def add_text(self, text):
        style = {}
        for s in self._text_style_stack:
            style.update(s)
        self._contents_stack[-1].append(
                docx.make_run(text, style, self._preserve_space))

    def add_break(self):
        self._contents_stack[-1].append(docx.make_break_run())

    def add_picture(self, rid, picid, filename, width, height, alt):
        self._contents_stack[-1].append(
                docx.make_inline_picture_run(
                    rid, picid, filename, width, height, alt))

    def add_footnote_reference(self, footnote_id, style_id):
        self._contents_stack[-1].append(
                docx.make_footnote_reference(footnote_id, style_id))

    def add_footnote_ref(self, style_id):
        self._contents_stack[-1].append(docx.make_footnote_ref(style_id))

    def add_textbox(self, style, color, contents, wrap_style=None):
        self._contents_stack[-1].append(docx.make_vml_textbox(
            style, color, (c.to_xml() for c in contents), wrap_style))

    def push_style(self, text_style):
        self._text_style_stack.append(text_style)

    def pop_style(self):
        self._text_style_stack.pop()

    def begin_hyperlink(self, hyperlink_style_id):
        self._contents_stack.append([])
        self._text_style_stack.append(
                docx.make_run_style_property(hyperlink_style_id))

    def end_hyperlink(self, rid, anchor):
        self._text_style_stack.pop()
        if rid is not None or anchor is not None:
            h = docx.make_hyperlink(rid, anchor)
            h.extend(self._contents_stack.pop())
            self._contents_stack[-1].append(h)
        else:
            run_list = self._contents_stack.pop()
            self._contents_stack[-1].extend(run_list)

    def keep_next(self):
        self._keep_next = True

    def append(self, contents):
        if isinstance(contents, Paragraph): # for nested line_block
            self._contents_stack[-1].extend(contents._contents_stack[0])
        elif isinstance(contents, BookmarkElement):
            self._contents_stack[-1].append(contents.to_xml())
        else:
            raise RuntimeError('Can not append %s' % to_error_string(contents))

    def to_xml(self):
        if self._style is not None:
            style_id = self._style
        else:
            style_id = type(self).default_style_id
        p = docx.make_paragraph(
                self._indent, self._right_indent, style_id, self._align,
                self._keep_lines, self._keep_next, self._list_info)
        p.extend(self._contents_stack[0])
        return p

class Table(TableElement):
    def __init__(
            self, table_style, table_width, colsize_list, indent, align,
            keep_next, cant_split_row, set_table_header, fit_content):
        self._style = table_style
        self._table_width = table_width
        self._colspec_list = []
        self._colsize_list = colsize_list
        self._indent = indent
        self._align = align
        self._stub = 0
        self._head = []
        self._body = []
        self._current_target = self._body
        self._current_row_index = -1
        self._current_cell_index = -1
         # 0: not set, 1: set header, 2: set first row, 3: set all rows
        self._keep_next = keep_next
        self._cant_split_row = cant_split_row
        self._set_table_header = set_table_header
        self._fit_content = fit_content

    @property
    def style(self):
        return self._style

    def keep_next(self):
        '''Set keep_next to set first row. This method is supposed to be
           called from only Table.make_cell.
        '''
        self._keep_next = 2

    def add_colspec(self, colspec):
        self._colspec_list.append(colspec)

    def add_stub(self):
        self._stub += 1

    def start_head(self):
        self._current_target = self._head
        self._current_row_index = -1

    def start_body(self):
        self._current_target = self._body
        self._current_row_index = -1

    def add_row(self):
        self._current_row_index += 1
        if self._current_row_index < len(self._current_target):
            row = self._current_target[self._current_row_index]
            for index, cell in enumerate(row):
                if cell is not None and cell[0] != 'continue':
                    self._current_cell_index = index - 1
                    break
            else:
                self._current_cell_index = index
        else:
            self._current_target.append([])
            self._current_cell_index = -1

    def add_cell(self, morerows, morecols):
        row = self._current_target[self._current_row_index]
        self._current_cell_index += (
                self._get_grid_span(row, self._current_cell_index))
        if not (self._current_cell_index < len(row)):
            row.append([None if morerows == 0 else 'restart', []])

        cell_index = self._current_cell_index
        start = cell_index + 1
        row[start:start + morecols] = (None for _ in range(morecols))

        for i in range(1, morerows + 1):
            if not (self._current_row_index + i < len(self._current_target)):
                self._current_target.append([])
            row = self._current_target[self._current_row_index + i]
            if cell_index < len(row):
                row[cell_index] = ['continue', []]
            else:
                row.extend([None, []] for _ in range(cell_index - len(row)))
                row.append(['continue', []])
            row[start:start + morecols] = (None for _ in range(morecols))

    def current_cell_width(self):
        if self._colspec_list:
            self._reset_colsize_list()
            self._colspec_list = []
        index = self._current_cell_index
        if not (index < len(self._colsize_list)):
            return None
        grid_span = self._get_grid_span(
                self._current_target[self._current_row_index], index)
        ratio = sum(self._colsize_list[index:index + grid_span])
        return int(self._table_width[0] * ratio)

    def append(self, contents):
        row = self._current_target[self._current_row_index]
        row[self._current_cell_index][1].append(contents)

    def to_xml(self):
        table = docx.make_table(
                self._style,
                None if self._fit_content else self._table_width[1],
                self._indent, self._align,
                (self._table_width[0] * col for col in self._colsize_list),
                self._head, self._stub > 0)
        for index, row in enumerate(self._head):
            table.append(self.make_row(index, row, True))
        for index, row in enumerate(self._body):
            table.append(self.make_row(index, row, False))
        return table

    def make_row(self, index, row, is_head):
        # Non-first header needs tblHeader to be applied first row style
        set_tbl_header = is_head and (self._set_table_header or index > 0)
        row_elem = docx.make_row(
                index, is_head, self._cant_split_row, set_tbl_header)
        keep_next = self._set_keep_next(is_head, index)
        for index, elem in enumerate(row):
            if elem is None: # Merged with the previous cell
                continue
            vmerge, cell = elem
            row_elem.append(self.make_cell(index, vmerge, cell, row, keep_next))
        return row_elem

    def make_cell(self, index, vmerge, cell, row, keep_next):
        grid_span = self._get_grid_span(row, index)
        if self._fit_content:
            cellsize = None
        else:
            cellsize = sum(self._colsize_list[index:index + grid_span])
        cell_elem = docx.make_cell(
                index, index < self._stub, cellsize, grid_span, vmerge)

        contents_types = (ParagraphElement, TableElement, SdtElement)
        # The last element must be paragraph for Microsoft word
        last = next(
                (e for e in reversed(cell) if isinstance(e, contents_types)),
                None)
        if last is None or isinstance(last, TableElement):
            cell.append(Paragraph())

        if keep_next:
            first = next(e for e in cell if isinstance(e, contents_types))
            first.keep_next()
        cell_elem.extend(c.to_xml() for c in cell)
        return cell_elem

    def _reset_colsize_list(self):
        total = float(sum(self._colspec_list))
        self._colsize_list = [colspec / total for colspec in self._colspec_list]

    def _get_grid_span(self, row, cell_index):
        grid_span = 1
        for cell in row[cell_index + 1:]:
            if cell is not None:
                break
            grid_span += 1
        return grid_span

    def _set_keep_next(self, is_head, index):
        if self._keep_next == 0:
            return False
        if self._keep_next == 1:
            return is_head
        if self._keep_next == 2:
            return (is_head or not self._head) and index == 0
        if self._keep_next == 3:
            return True
        return False

class TOC(SdtElement):
    def __init__(
            self, title, title_style_id, maxlevel, bookmark, paragraph_width,
            outlines):
        self._title = title
        self._title_style_id = title_style_id
        self._maxlevel = maxlevel
        self._bookmark = bookmark
        self._paragraph_width = paragraph_width
        self._outlines = outlines

    def to_xml(self):
        return docx.make_table_of_contents(
                self._title, self._title_style_id,
                self._maxlevel, self._bookmark, self._paragraph_width,
                self._outlines)

class Document(object):
    def __init__(self, body, sect_props):
        self._body = body
        self._add_pagebreak = False
        self._default_orient = docx.get_orient(sect_props[0])
        self._sect_props = {
                self._default_orient: sect_props[0],
                docx.get_orient(sect_props[1]): sect_props[1],
        }
        self._current_orient = self._default_orient
        self._last_orient = None

    def add_pagebreak(self):
        self._add_pagebreak = True

    def add_last_section_property(self):
        if self._last_orient is not None:
            orient = self._last_orient
        else:
            orient = self._current_orient
        self._body.append(self._sect_props[orient])

    def set_page_oriented(self, orient=None):
        if orient is None:
            orient = self._default_orient
        if self._current_orient != orient:
            if self._last_orient is not None:
                # last_orient must be equal to orient, then addition of section
                # property is enable to be postponed
                self._last_orient = None
            else:
                self._last_orient = self._current_orient
            self._current_orient = orient

    def get_current_page_width(self):
        return docx.get_contents_width(self._sect_props[self._current_orient])

    def get_current_page_height(self):
        return docx.get_contents_height(self._sect_props[self._current_orient])

    def append(self, contents):
        xml = contents.to_xml()
        if not isinstance(contents, BookmarkElement):
            self._add_section_prop_if_necessary()
            if self._add_pagebreak:
                docx.add_page_break_before_to_first_paragraph(xml)
                self._add_pagebreak = False
        self._body.append(xml)

    def _add_section_prop_if_necessary(self):
        if self._last_orient is not None:
            self._body.append(docx.make_section_prop_paragraph(
                self._sect_props[self._last_orient]))
            for sect_prop in self._sect_props.values():
                docx.set_title_page(sect_prop, False)
                docx.set_title_page(sect_prop, False)
            self._last_orient = None

class LiteralBlock(ParagraphElement):
    def __init__(self, highlighted, style_id, indent, right_indent, keep_lines):
        self._args = [highlighted, style_id, indent, right_indent, keep_lines]
        self._keep_next = False

    def keep_next(self):
        self._keep_next = True

    def to_xml(self):
        highlighted, style_id, indent, right_indent, keep_lines = self._args
        p = docx.make_paragraph(
                indent, right_indent, style_id, None,
                keep_lines, self._keep_next, None)
        p.extend(etree.fromstring(highlighted))
        return p

class LiteralBlockTable(TableElement):
    def __init__(
            self, highlighted, top_space,
            style_id, table_width, indent, keep_next):
        self._args = [highlighted, top_space, style_id, table_width, indent]
         # 0: not set, 1: set header, 2: set first row, 3: set all rows
        self._keep_next = 3 if keep_next else 0

    def keep_next(self):
        self._keep_next = max(2, self._keep_next)

    def to_xml(self):
        highlighted, top_space, style_id, table_width, indent = self._args
        org_tbl = etree.fromstring(highlighted)
        table = docx.make_table(
                None, table_width[1], indent, None,
                [table_width[0] * 0.1, table_width[0] * 0.9], False, True,
                properties=[
                    docx.make_table_cell_spacing_property(None),
                    docx.make_table_cell_margin_property(
                        top=None, left=108, bottom=None, right=108),
                ])
        no_spacing = docx.make_paragraph_spacing_property(before=0, after=0)
        shading = docx.make_paragraph_shading_property('clear')
        lineno_border = docx.make_paragraph_border_property(
                top=None, bottom=None, left=None, right=None)
        middle_border = docx.make_paragraph_border_property(
                top=None, bottom=None)
        last_index = len(org_tbl) - 1
        if last_index == 0:
            border = {0: docx.make_paragraph_border_property()}
        else:
            border = {
                    0: docx.make_paragraph_border_property(bottom=None),
                    last_index: docx.make_paragraph_border_property(top=None),
            }

        for index, org_row in enumerate(org_tbl):
            row = docx.make_row(index, False, False, False)
            if index == 0:
                spacing = docx.make_paragraph_spacing_property(
                        before=(top_space or 0), after=0)
            else:
                spacing = no_spacing
            cell1 = docx.make_cell(0, True, None, 1, None, valign='top')
            keep_next = self._is_keep_next(index)
            p1 = docx.make_paragraph(
                    None, None, style_id, 'right', False, keep_next, None,
                    properties=[spacing, shading, lineno_border])
            p1.extend(org_row[0][0])
            cell1.append(p1)
            row.append(cell1)

            cell2 = docx.make_cell(1, False, 0.99, 1, None, valign='top')
            p2 = docx.make_paragraph(
                    None, None, style_id, None, False, False, None,
                    properties=[no_spacing, border.get(index, middle_border)])
            p2.extend(org_row[1][0])
            cell2.append(p2)
            row.append(cell2)
            table.append(row)
        return table

    def _is_keep_next(self, index):
        return (self._keep_next == 2 and index == 0) or (self._keep_next == 3)

class ContentsList(object):
    def __init__(self):
        self._contents_list = []

    def append(self, contents):
        self._contents_list.append(contents)

    def __iter__(self):
        return iter(self._contents_list)

    def __len__(self):
        return len(self._contents_list)

    def __getitem__(self, key):
        return self._contents_list[key]

class FixedTopParagraphList(ContentsList):
    def __init__(self, top_paragraph):
        super(FixedTopParagraphList, self).__init__()
        self._top_paragraph = top_paragraph
        self._available_top_paragraph = True
        super(FixedTopParagraphList, self).append(self._top_paragraph)

    def append(self, contents):
        if len(self) == 1:
            if isinstance(contents, BookmarkElement):
                self._top_paragraph.append(contents)
                return
            if self._available_top_paragraph:
                if isinstance(contents, Paragraph) and contents._style is None:
                    self._top_paragraph.append(contents)
                    return
                else:
                    self._available_top_paragraph = False
        super(FixedTopParagraphList, self).append(contents)

class DefinitionListItem(ContentsList):
    def __init__(self):
        super(DefinitionListItem, self).__init__()
        self._last_term = None

    @property
    def last_term(self):
        return self._last_term

    def add_term(self, term_paragraph):
        self._contents_list.append(term_paragraph)
        self._last_term = term_paragraph

class Contenxt(object):
    def __init__(self, indent, right_indent, width, list_level):
        self.indent = indent
        self.right_indent = right_indent
        self.width = width
        self.list_level = list_level

    @property
    def paragraph_width(self):
        return self.width - self.indent - self.right_indent

class DocxTranslator(nodes.NodeVisitor):
    def __init__(self, document, builder):
        nodes.NodeVisitor.__init__(self, document)
        self._builder = builder
        self.builder = self._builder # Needs for graphviz.render_dot
        stylefile = builder.config['docx_style']
        if stylefile:
            stylefile = os.path.join(builder.confdir, os.path.join(stylefile))
        else: # Use default style file
            stylefile = os.path.join(
                    os.path.dirname(__file__), 'docx/style.docx')
        self._docx = docx.DocxComposer(
                stylefile, builder.config['docx_coverpage'])
        self._doc_stack = [
                Document(
                    self._docx.docbody,
                    self._docx.get_each_orient_section_properties())
        ]
        self._docname_stack = []
        self._section_level = 0
        self._ctx_stack = [
                Contenxt(0, 0, self._doc_stack[-1].get_current_page_width(), 0)
        ]
        self._relationship_stack = ['document']
        self._line_block_level = 0
        self._list_id_stack = []
        self._basic_indent = self._docx.get_indent('List Paragraph', 320)
        self._language = builder.config.highlight_language
        self._linenothreshold = sys.maxsize
        self._highlighter = DocxPygmentsBridge(
                'html',
                builder.config.pygments_style,
                builder.config.trim_doctest_flags)
        self._numsec_map = builder.make_numsec_map()
        self._numfig_map = builder.make_numfig_map()
        self._bookmark_id = 0
        self._bookmark_id_map = {} # bookmark name => BookmarkStart id
        self._logger = logging.getLogger('docxbuilder')

        self._create_docxbuilder_styles()
        self._bullet_list_id = self._docx.get_bullet_list_num_id('List Bullet')
        self._bullet_list_indents = self._docx.get_numbering_left('List Bullet')
        self._number_list_indent = self._docx.get_numbering_left('List Number')[0]
        Paragraph.default_style_id = self._docx.get_style_id('Body Text')

    def asbytes(self):
        props = self._builder.doc_properties
        core_props, custom_props, invalid_prop_keys = (
                docx.separate_core_and_custom_properties(props))
        for key in invalid_prop_keys:
            self._builder._logger.warning(
                    'invalid value is found in docx_documents "%s"' % key)
        core_props.setdefault('language', self._builder.config.language or 'en')
        return self._docx.asbytes(core_props, custom_props)

    def _pop_and_append(self):
        contents = self._doc_stack.pop()
        if isinstance(contents, ContentsList):
            for c in contents:
                self._doc_stack[-1].append(c)
        else:
            self._doc_stack[-1].append(contents)

    def _append_bookmark_start(self, ids):
        docname = self._docname_stack[-1]
        for id in ids:
            name = make_bookmark_name(docname, id)
            self._bookmark_id += 1
            self._bookmark_id_map[name] = self._bookmark_id
            self._doc_stack[-1].append(BookmarkStart(self._bookmark_id, name))

    def _append_bookmark_end(self, ids):
        docname = self._docname_stack[-1]
        for id in ids:
            name = make_bookmark_name(docname, id)
            bookmark_id = self._bookmark_id_map.pop(name, None)
            if bookmark_id is None:
                continue
            self._doc_stack[-1].append(BookmarkEnd(bookmark_id))

    def _make_paragraph(
            self, indent=None, right_indent=None, style=None, align=None,
            keep_lines=False, keep_next=False,
            list_info=None, preserve_space=False):
        style_id = self._docx.get_style_id(style) if style is not None else None
        return Paragraph(
                indent, right_indent, style_id, align, keep_lines, keep_next,
                list_info, preserve_space)

    def _append_table(
            self, table_style, table_width, colsize_list, is_indent, align=None,
            in_single_page=False, row_splittable=True,
            header_in_all_page=False, fit_content=False):
        if table_style is not None:
            table_style = self._docx.get_style_id(table_style)
        indent = self._ctx_stack[-1].indent if is_indent else 0
        keep_next = 3 if in_single_page else 1
        t = Table(
                table_style,
                (table_width, float(table_width) / self._ctx_stack[-1].width),
                colsize_list, indent, align,
                keep_next, not row_splittable, header_in_all_page, fit_content)
        self._doc_stack.append(t)
        self._append_new_ctx(indent=0, right_indent=0, width=table_width)
        return t

    def _pop_and_append_table(self):
        self._ctx_stack.pop()
        self._pop_and_append()
        # Append a paragaph as a margin between the table and the next element
        self._doc_stack[-1].append(
                self._make_paragraph(style='Table Bottom Margin'))

    def _add_table_cell(self, morerows=0, morecols=0):
        t = self._doc_stack[-1]
        t.add_cell(morerows, morecols)
        width = t.current_cell_width()
        if width is not None:
            margin = self._docx.get_table_cell_margin(t.style)
            self._ctx_stack[-1].width = width - margin

    def _push_style(self, style_name, based_style_name=None):
        if based_style_name is not None:
            self._docx.create_style(
                    'character', style_name, based_style_name, True, False)
        style_id = self._docx.get_style_id(style_name)
        if self._builder.config.docx_nested_character_style:
            style = self._docx.get_run_style_property(style_id)
        else:
            style = docx.make_run_style_property(style_id)
        self._doc_stack[-1].push_style(style)

    def _append_new_ctx(
            self, indent=None, right_indent=None, width=None):
        if indent is None:
            indent = self._ctx_stack[-1].indent
        if right_indent is None:
            right_indent = self._ctx_stack[-1].right_indent
        if width is None:
            width = self._ctx_stack[-1].width
        self._ctx_stack.append(Contenxt(indent, right_indent, width, 0))

    def _set_page_oriented(self):
        self._doc_stack[-1].set_page_oriented('landscape')
        self._append_new_ctx(
                indent=0, right_indent=0,
                width=self._doc_stack[-1].get_current_page_width())

    def _clear_page_oriented(self):
        self._ctx_stack.pop()
        self._doc_stack[-1].set_page_oriented()

    def _get_numsec(self, ids):
        for id in ids:
            num = self._numsec_map.get('%s/#%s' % (self._docname_stack[-1], id))
            if num:
                return '.'.join(map(str, num)) + ' '
        else:
            # First section of each file has no hash
            num = self._numsec_map.get('%s/' % self._docname_stack[-1], None)
            if num:
                return '.'.join(map(str, num)) + ' '
        return None

    def _get_numfig(self, figtype, ids):
        item = self._numfig_map.get(figtype)
        if item is None:
            return None
        prefix, num_map = item
        if prefix is None:
            return None
        for id in ids:
            num = num_map.get('%s/%s' % (self._docname_stack[-1], id))
            if num:
                return prefix % ('.'.join(map(str, num)) + ' ')
        return None

    def _get_table_option(self, classes, option, default_value):
        if ('docx-%s' % option) in classes:
            return True
        if ('docx-no-%s' % option) in classes:
            return False
        return self._builder.config.docx_table_options.get(
                option.replace('-', '_'), default_value)

    def _is_landscape_table(self, node):
        if not isinstance(self._doc_stack[-1], Document):
            return False
        option = self._get_table_option(node.get('classes'), 'landscape', None)
        if option is not None:
            return option
        landscape_columns = self._builder.config.docx_table_options.get(
                'landscape_columns', 0)
        if landscape_columns < 1:
            return False
        return landscape_columns <= count_colspec(node)

    def _is_landscape_figure(self, node):
        if not isinstance(self._doc_stack[-1], Document):
            return False
        return 'docx-landscape' in node.get('classes')

    def visit_admonition_node(self, node, add_title=False):
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack.append(ContentsList())
        if add_title:
            p = self._make_paragraph()
            p.add_text(admonitionlabels[node.tagname] + ':')
            self._doc_stack[-1].append(p)

    def depart_admonition_node(
            self, node, style=None, align='center', margin=1000):
        contents = self._doc_stack.pop()
        table_width = self._ctx_stack[-1].width - margin
        if style is None:
            style = next((
                ' '.join(word.capitalize() for word in c.split('-'))
                for c in node.get('classes') if c.startswith('admonition-')),
                'Admonition %s' % node.tagname.capitalize())
            self._docx.create_style('table', style, 'Based Admonition', True)
        if align is None:
            is_indent = True
        else:
            is_indent = False
        t = self._append_table(
                style, table_width, [1.0], is_indent, align, fit_content=False)
        t.start_head()
        t.add_row()
        self._add_table_cell()
        for idx, c in enumerate(contents):
            t.append(c)
            if not isinstance(c, BookmarkElement):
                break
        idx = idx + 1
        for idx, c in enumerate(contents[idx:], idx):
            if not isinstance(c, BookmarkEnd):
                break
            t.append(c)
        body_contents = contents[idx:]
        if body_contents:
            t.start_body()
            t.add_row()
            self._add_table_cell()
            for c in body_contents:
                t.append(c)
        self._pop_and_append_table()
        self._append_bookmark_end(node.get('ids', []))

    def visit_image_node(self, node, alt, get_filepath):
        self._append_bookmark_start(node.get('ids', []))

        if not isinstance(self._doc_stack[-1], Paragraph):
            if isinstance(node.parent, nodes.figure):
                style = 'Figure'
                align = node.parent.get('align')
                keep_next = has_caption(node)
            else:
                style = None
                align = None
                keep_next = False
            self._doc_stack.append(self._make_paragraph(
                self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent,
                style=style, align=align, keep_next=keep_next))
            needs_pop = True
        else:
            needs_pop = False

        if isinstance(alt, tuple):
            alt, alt_lang = alt
        else:
            alt_lang = None
        try:
            filepath = get_filepath(self, node)
            width, height = self._get_image_scaled_size(node, filepath)
            rid = self._docx.add_image_relationship(
                    filepath, self._relationship_stack[-1])
            filename = os.path.basename(filepath)
            self._doc_stack[-1].add_picture(
                    rid, self._docx.new_id(), filename, width, height, alt)
        except Exception as e:
            self._logger.warning(e, location=node)
            if alt_lang is not None and needs_pop:
                highlighted = self._highlighter.highlight_block(alt, alt_lang)
                literal_block = LiteralBlock(
                        highlighted,
                        self._docx.get_style_id('LiteralBlock'), 0, 0, False)
                width = convert_to_cm_size(self._ctx_stack[-1].paragraph_width)
                self._doc_stack[-1].add_textbox(
                        'width:%fcm' % width, 'white', [literal_block])
            else:
                self._push_style('Problematic')
                self._doc_stack[-1].add_text(alt)
                self._doc_stack[-1].pop_style()

        if needs_pop:
            self._pop_and_append()

        self._append_bookmark_end(node.get('ids', []))
        raise nodes.SkipNode

    def visit_math_block_node(self, node, latex):
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack.append(self._make_paragraph(
            self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent,
            'Math Block'))
        self._doc_stack[-1].add_text(latex) # TODO
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))
        raise nodes.SkipNode

    def visit_start_of_file(self, node):
        self._docname_stack.append(node['docname'])
        self._append_bookmark_start([''])
        self._append_bookmark_start(node.get('ids', []))

    def depart_start_of_file(self, node):
        self._append_bookmark_end(node.get('ids', []))
        self._append_bookmark_end([''])
        self._docname_stack.pop()

    def visit_Text(self, node):
        self._doc_stack[-1].add_text(node.astext())

    def depart_Text(self, node):
        pass

    def visit_document(self, node):
        self._docname_stack.append(node['docname'])
        self._append_bookmark_start([''])

    def depart_document(self, node):
        self._append_bookmark_end([''])
        self._docname_stack.pop()
        self._doc_stack[-1].add_last_section_property()

    def visit_title(self, node):
        self._append_bookmark_start(node.get('ids', []))
        if isinstance(node.parent, nodes.table):
            style = 'Table Caption'
            title_num = self._get_numfig('table', node.parent['ids'])
            indent = self._ctx_stack[-1].indent
            right_indent = self._ctx_stack[-1].right_indent
            align = node.parent.get('align')
        elif isinstance(node.parent, nodes.section):
            style = 'Heading %d' % self._section_level
            self._docx.create_style('paragraph', style, 'Heading', False)
            title_num = self._get_numsec(node.parent['ids'])
            indent = None
            right_indent = None
            align = None
        elif isinstance(node.parent, nodes.Admonition):
            style = None # admonition's style is customized by Admonition
            title_num = None
            indent = self._ctx_stack[-1].indent
            right_indent = self._ctx_stack[-1].right_indent
            align = None
        else:
            style = '%s Title Heading' % node.tagname.capitalize()
            self._docx.create_style('paragraph', style, 'Title Heading', True)
            title_num = None
            indent = self._ctx_stack[-1].indent
            right_indent = self._ctx_stack[-1].right_indent
            align = None
        self._doc_stack.append(self._make_paragraph(
            indent, right_indent, style, align, keep_next=True))
        if title_num is not None:
            self._doc_stack[-1].add_text(title_num)

    def depart_title(self, node):
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_subtitle(self, node):
        self._append_bookmark_start(node.get('ids', []))
        style = '%s Subtitle Heading' % node.tagname.capitalize()
        self._docx.create_style('paragraph', style, 'Subtitle Heading', True)
        self._doc_stack.append(self._make_paragraph(
            self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent,
            style))

    def depart_subtitle(self, node):
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_section(self, node):
        config = self._builder.config
        if (self._section_level < config.docx_pagebreak_before_section
                and isinstance(self._doc_stack[-1], Document)):
            self._doc_stack[-1].add_pagebreak()
        self._append_bookmark_start(node.get('ids', []))
        self._section_level += 1

    def depart_section(self, node):
        self._section_level -= 1
        self._append_bookmark_end(node.get('ids', []))

    def visit_topic(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._append_new_ctx(width=self._ctx_stack[-1].paragraph_width - 100)
        self._doc_stack.append(ContentsList())

    def depart_topic(self, node):
        width = convert_to_cm_size(self._ctx_stack[-1].paragraph_width)
        self._ctx_stack.pop()
        p = self._make_paragraph(
            self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent,
            align='center')
        # TODO: enable to configure color
        p.add_textbox('width:%fcm' % width, '#ddeeff', self._doc_stack.pop())
        self._doc_stack[-1].append(p)
        self._append_bookmark_end(node.get('ids', []))

    def visit_sidebar(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._append_new_ctx(width=self._ctx_stack[-1].paragraph_width / 2)
        self._doc_stack.append(ContentsList())

    def depart_sidebar(self, node):
        # TODO: enable to configure color, width, and position
        width = convert_to_cm_size(self._ctx_stack[-1].paragraph_width)
        self._ctx_stack.pop()
        style = ';'.join([
                'width:%fcm' % width,
                'mso-position-horizontal:right',
                'mso-position-vertical-relative:text',
                'position:absolute',
        ])
        wrap_style = {'type': 'square', 'anchory': 'text', 'side': 'left',}
        p = self._make_paragraph()
        p.add_textbox(style, '#ddeeff', self._doc_stack.pop(), wrap_style)
        self._doc_stack[-1].append(p)
        self._append_bookmark_end(node.get('ids', []))

    def visit_transition(self, node):
        self._doc_stack[-1].append(self._make_paragraph(style='Transition'))

    def depart_transition(self, node):
        pass

    def visit_paragraph(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack.append(self._make_paragraph(
            self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent))

    def depart_paragraph(self, node):
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_compound(self, node):
        self._append_bookmark_start(node.get('ids', []))

    def depart_compound(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_container(self, node):
        self._append_bookmark_start(node.get('ids', []))

    def depart_container(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_literal_block(self, node):
        self._append_bookmark_start(node.get('ids', []))
        text = node.astext()
        keep_lines = (text.count('\n') + 1 < 20)
        if node.rawsource != text: # Maybe parsed-literal
            self._doc_stack.append(self._make_paragraph(
                self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent,
                'Literal Block', keep_lines=keep_lines, preserve_space=True))
            return
        else:
            language = node.get('language', self._language)
            linenos = node.get(
                    'linenos',
                    (node.rawsource.count('\n') >= self._linenothreshold - 1))
            highlight_args = node.get('highlight_args', {})
            config = self._builder.config
            opts = (config.highlight_options
                    if language == config.highlight_language else {})
            highlighted = self._highlighter.highlight_block(
                    node.rawsource, language,
                    linenos=linenos, opts=opts, location=node, **highlight_args)
            style_id = self._docx.get_style_id('Literal Block')
            ctx = self._ctx_stack[-1]
            if linenos:
                table_width = ctx.paragraph_width
                border_info = self._docx.get_border_info(style_id, 'top')
                if border_info is not None:
                    top_space = int(
                            border_info.get('size', 1) * 2.5 +
                            border_info.get('space', 0) * 20)
                else:
                    top_space = 0
                block = LiteralBlockTable(
                        highlighted, top_space, style_id,
                        (table_width, float(table_width) / ctx.width),
                        ctx.indent, keep_lines)
            else:
                block = LiteralBlock(
                        highlighted, style_id,
                        ctx.indent, ctx.right_indent, keep_lines)
            self._doc_stack.append(block)
            raise nodes.SkipChildren

    def depart_literal_block(self, node):
        if isinstance(self._doc_stack[-1], LiteralBlockTable):
            self._pop_and_append()
            self._doc_stack[-1].append(
                    self._make_paragraph(style='Table Bottom Margin'))
        else:
            self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_doctest_block(self, node):
        org_lang = self._language
        self._language = 'python3'
        try:
            self.visit_literal_block(node)
        finally:
            self._language = org_lang

    def depart_doctest_block(self, node):
        self.depart_literal_block(node)

    def visit_math_block(self, node):
        self.visit_math_block_node(node, node.astext())

    def visit_line_block(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack.append(self._make_paragraph(
            self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent))
        self._line_block_level += 1

    def depart_line_block(self, node):
        self._line_block_level -= 1
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_line(self, node):
        self._append_bookmark_start(node.get('ids', []))
        indent = ''.join('    ' for _ in range(self._line_block_level - 1))
        self._doc_stack[-1].add_text(indent)

    def depart_line(self, node):
        self._doc_stack[-1].add_break()
        self._append_bookmark_end(node.get('ids', []))

    def visit_block_quote(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._ctx_stack[-1].indent += self._basic_indent

    def depart_block_quote(self, node):
        self._ctx_stack[-1].indent -= self._basic_indent
        self._append_bookmark_end(node.get('ids', []))

    def visit_attribution(self, node):
        self._append_bookmark_start(node.get('ids', []))
        p = self._make_paragraph(
                self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent)
        p.add_text(u'— ')
        self._doc_stack.append(p)

    def depart_attribution(self, node):
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_table(self, node):
        if self._is_landscape_table(node):
            self._set_page_oriented()
        self._append_bookmark_start(node.get('ids', []))

    def depart_table(self, node):
        self._append_bookmark_end(node.get('ids', []))
        if self._is_landscape_table(node):
            self._clear_page_oriented()

    def visit_tgroup(self, node):
        self._append_bookmark_start(node.get('ids', []))
        align = node.parent.get('align')
        classes = node.parent.get('classes')
        self._append_table(
                'Table',
                self._ctx_stack[-1].paragraph_width, [1.0], True, align,
                in_single_page=self._get_table_option(
                    classes, 'in-single-page', False),
                row_splittable=self._get_table_option(
                    classes, 'row-splittable', True),
                header_in_all_page=self._get_table_option(
                    classes, 'header-in-all-page', False),
                fit_content=('colwidths-auto' in classes))

    def depart_tgroup(self, node):
        self._pop_and_append_table()
        self._append_bookmark_end(node.get('ids', []))

    def visit_colspec(self, node):
        self._append_bookmark_start(node.get('ids', []))
        table = self._doc_stack[-1]
        table.add_colspec(node['colwidth'])
        if node.get('stub', 0) == 1:
            table.add_stub()

    def depart_colspec(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_thead(self, node):
        self._append_bookmark_start(node.get('ids', []))
        table = self._doc_stack[-1]
        table.start_head()

    def depart_thead(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_tbody(self, node):
        self._append_bookmark_start(node.get('ids', []))
        table = self._doc_stack[-1]
        table.start_body()

    def depart_tbody(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_row(self, node):
        self._append_bookmark_start(node.get('ids', []))
        table = self._doc_stack[-1]
        table.add_row()

    def depart_row(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_entry(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._add_table_cell(node.get('morerows', 0), node.get('morecols', 0))

    def depart_entry(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_figure(self, node):
        if self._is_landscape_figure(node):
            self._set_page_oriented()
        self._append_bookmark_start(node.get('ids', []))
        paragraph_width = self._ctx_stack[-1].paragraph_width
        width = convert_to_twip_size(node.get('width', '100%'), paragraph_width)
        delta_width = paragraph_width - width
        align = node.get('align', 'left')
        if align == 'left':
            self._append_new_ctx(
                right_indent=self._ctx_stack[-1].right_indent + delta_width)
        elif align == 'center':
            padding = delta_width // 2
            self._append_new_ctx(
                indent=self._ctx_stack[-1].indent + padding,
                right_indent=self._ctx_stack[-1].right_indent + padding)
        elif align == 'right':
            self._append_new_ctx(
                indent=self._ctx_stack[-1].indent + delta_width)

    def depart_figure(self, node):
        self._ctx_stack.pop()
        self._append_bookmark_end(node.get('ids', []))
        if self._is_landscape_figure(node):
            self._clear_page_oriented()

    def visit_caption(self, node):
        self._append_bookmark_start(node.get('ids', []))
        if isinstance(node.parent, nodes.figure):
            style = 'Image Caption'
            figtype = 'figure'
            align = node.parent.get('align')
            keep_next = False
        else:
            style = 'Literal Caption'
            figtype = 'code-block'
            align = None
            keep_next = True
        self._doc_stack.append(self._make_paragraph(
            self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent, style,
            align, keep_next=keep_next))
        caption_num = self._get_numfig(figtype, node.parent['ids'])
        if caption_num is not None:
            self._doc_stack[-1].add_text(caption_num)

    def depart_caption(self, node):
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_legend(self, node):
        self._append_bookmark_start(node.get('ids', []))

    def depart_legend(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_footnote(self, node):
        self._relationship_stack.append('footnotes')
        p = self._make_paragraph(None, None, 'Footnote Text')
        p.add_footnote_ref(self._docx.get_style_id('Footnote Reference'))
        p.add_text(' ')
        self._doc_stack.append(FixedTopParagraphList(p))
        self._append_bookmark_start(node.get('ids', []))

    def depart_footnote(self, node):
        self._append_bookmark_end(node.get('ids', []))
        footnote = self._doc_stack.pop()
        prev_fid = None
        for id in node.get('ids'):
            fid = self._docx.set_default_footnote_id(
                    '%s#%s' % (self._docname_stack[-1], id), prev_fid)
            if fid != prev_fid:
                self._docx.append_footnote(fid, (c.to_xml() for c in footnote))
                prev_fid = fid
        self._relationship_stack.pop()

    def visit_citation(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack.append(FixedTopParagraphList(self._make_paragraph(
            self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent,
            style='Bibliography')))

    def depart_citation(self, node):
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_label(self, node):
        if isinstance(node.parent, nodes.footnote):
            raise nodes.SkipNode
        if isinstance(node.parent, nodes.citation):
            self._doc_stack[-1][0].add_text('[%s] ' % node.astext())
            raise nodes.SkipNode
        pass

    def depart_label(self, node):
        pass

    def visit_rubric(self, node):
        if node.astext() in ('Footnotes', _('Footnotes')):
            raise nodes.SkipNode
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack.append(self._make_paragraph(
            self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent,
            'Rubric Title Heading'))

    def depart_rubric(self, node):
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_bullet_list(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._ctx_stack[-1].list_level += 1
        self._ctx_stack[-1].indent += self._get_additional_list_indent(
                self._ctx_stack[-1].list_level - 1)

    def depart_bullet_list(self, node):
        self._ctx_stack[-1].indent -= self._get_additional_list_indent(
                self._ctx_stack[-1].list_level - 1)
        self._ctx_stack[-1].list_level -= 1
        self._append_bookmark_end(node.get('ids', []))

    def visit_enumerated_list(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._ctx_stack[-1].indent += self._number_list_indent
        enumtype = node.get('enumtype', 'arabic')
        prefix = node.get('prefix', '')
        suffix = node.get('suffix', '')
        start = node.get('start', 1)
        self._list_id_stack.append(self._docx.add_numbering_style(
            start, '{}%1{}'.format(prefix, suffix), enumtype,
            self._number_list_indent))

    def depart_enumerated_list(self, node):
        self._ctx_stack[-1].indent -= self._number_list_indent
        self._list_id_stack.pop()
        self._append_bookmark_end(node.get('ids', []))

    def visit_list_item(self, node):
        self._append_bookmark_start(node.get('ids', []))
        if isinstance(node.parent, nodes.enumerated_list):
            style = 'List Number'
            list_info=(self._list_id_stack[-1], 0)
        else:
            style = 'List Bullet'
            if self._bullet_list_id is not None:
                max_level = max(len(self._bullet_list_indents) - 1, 0)
                list_indent_level = min(
                        self._ctx_stack[-1].list_level - 1, max_level)
                list_info=(self._bullet_list_id, list_indent_level)
            else:
                list_info=None
        self._doc_stack.append(FixedTopParagraphList(
            self._make_paragraph(
                self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent,
                style, list_info=list_info)))

    def depart_list_item(self, node):
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_definition_list(self, node):
        self._append_bookmark_start(node.get('ids', []))

    def depart_definition_list(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_definition_list_item(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack.append(DefinitionListItem())

    def depart_definition_list_item(self, node):
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_term(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack.append(self._make_paragraph(
            self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent,
            'Definition Term', keep_next=True))

    def depart_term(self, node):
        term_paragraph = self._doc_stack.pop()
        self._doc_stack[-1].add_term(term_paragraph)
        self._append_bookmark_end(node.get('ids', []))

    def visit_classifier(self, node):
        self._append_bookmark_start(node.get('ids', []))
        term_paragraph = self._doc_stack[-1].last_term
        self._doc_stack.append(term_paragraph)
        term_paragraph.add_text(' : ')

    def depart_classifier(self, node):
        self._doc_stack.pop()
        self._append_bookmark_end(node.get('ids', []))

    def visit_definition(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._ctx_stack[-1].indent += self._basic_indent

    def depart_definition(self, node):
        self._ctx_stack[-1].indent -= self._basic_indent
        self._append_bookmark_end(node.get('ids', []))

    def visit_field_list(self, node):
        self._append_bookmark_start(node.get('ids', []))
        table_width = self._ctx_stack[-1].paragraph_width
        table = self._append_table(
                'Field List', table_width, [0.25, 0.75], True, fit_content=True)
        table.add_stub()

    def depart_field_list(self, node):
        self._pop_and_append_table()
        self._append_bookmark_end(node.get('ids', []))

    def visit_field(self, node):
        self._append_bookmark_start(node.get('ids', []))
        table = self._doc_stack[-1]
        table.add_row()

    def depart_field(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_field_name(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._add_table_cell()
        self._doc_stack.append(self._make_paragraph())

    def depart_field_name(self, node):
        self._doc_stack[-1].add_text(':')
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_field_body(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._add_table_cell()

    def depart_field_body(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_option_list(self, node):
        self._append_bookmark_start(node.get('ids', []))
        table_width = self._ctx_stack[-1].paragraph_width - 500
        self._append_table(
                'Option List', table_width, [1.0], True, fit_content=False)

    def depart_option_list(self, node):
        self._pop_and_append_table()
        self._append_bookmark_end(node.get('ids', []))

    def visit_option_list_item(self, node):
        self._append_bookmark_start(node.get('ids', []))

    def depart_option_list_item(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_option_group(self, node):
        self._append_bookmark_start(node.get('ids', []))
        table = self._doc_stack[-1]
        table.add_row()
        self._add_table_cell()
        self._doc_stack.append(self._make_paragraph(0, keep_next=True))

    def depart_option_group(self, node):
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_option(self, node):
        self._append_bookmark_start(node.get('ids', []))
        parent = node.parent
        first_option_index = parent.first_child_matching_class(nodes.option)
        if parent[first_option_index] is not node:
            self._doc_stack[-1].add_text(', ')

    def depart_option(self, node):
        self._append_bookmark_end(node.get('ids', []))
        pass

    def visit_option_string(self, node):
        self._append_bookmark_start(node.get('ids', []))

    def depart_option_string(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_option_argument(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack[-1].add_text(node.get('delimiter', ' '))
        self._push_style('Option Argument', 'Emphasis')

    def depart_option_argument(self, node):
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_description(self, node):
        self._append_bookmark_start(node.get('ids', []))
        table = self._doc_stack[-1]
        table.add_row()
        self._add_table_cell()
        self._ctx_stack[-1].indent += self._basic_indent

    def depart_description(self, node):
        self._ctx_stack[-1].indent -= self._basic_indent
        self._append_bookmark_end(node.get('ids', []))

    def visit_attention(self, node):
        self.visit_admonition_node(node, add_title=True)

    def depart_attention(self, node):
        self.depart_admonition_node(node)

    def visit_caution(self, node):
        self.visit_admonition_node(node, add_title=True)

    def depart_caution(self, node):
        self.depart_admonition_node(node)

    def visit_danger(self, node):
        self.visit_admonition_node(node, add_title=True)

    def depart_danger(self, node):
        self.depart_admonition_node(node)

    def visit_error(self, node):
        self.visit_admonition_node(node, add_title=True)

    def depart_error(self, node):
        self.depart_admonition_node(node)

    def visit_hint(self, node):
        self.visit_admonition_node(node, add_title=True)

    def depart_hint(self, node):
        self.depart_admonition_node(node)

    def visit_important(self, node):
        self.visit_admonition_node(node, add_title=True)

    def depart_important(self, node):
        self.depart_admonition_node(node)

    def visit_note(self, node):
        self.visit_admonition_node(node, add_title=True)

    def depart_note(self, node):
        self.depart_admonition_node(node)

    def visit_tip(self, node):
        self.visit_admonition_node(node, add_title=True)

    def depart_tip(self, node):
        self.depart_admonition_node(node)

    def visit_warning(self, node):
        self.visit_admonition_node(node, add_title=True)

    def depart_warning(self, node):
        self.depart_admonition_node(node)

    def visit_admonition(self, node):
        self.visit_admonition_node(node)

    def depart_admonition(self, node):
        self.depart_admonition_node(node, 'Admonition')

    def visit_substitution_definition(self, node):
        raise nodes.SkipNode # TODO

    def visit_comment(self, node):
        raise nodes.SkipNode # TODO

    def visit_pending(self, node):
        raise nodes.SkipNode # TODO

    def visit_system_message(self, node):
        raise nodes.SkipNode # TODO


    def visit_emphasis(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Emphasis')

    def depart_emphasis(self, node):
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_strong(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Strong')

    def depart_strong(self, node):
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_literal(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Literal')

    def depart_literal(self, node):
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_math(self, node):
        self._append_bookmark_start(node.get('ids', []))
        latex = node.get('latex')
        if latex:
            self._doc_stack[-1].add_text(latex)
            raise nodes.SkipChildren
        pass # TODO

    def depart_math(self, node):
        self._append_bookmark_end(node.get('ids', []))
        pass

    def visit_reference(self, node):
        self._append_bookmark_start(node.get('ids', []))
        if not isinstance(self._doc_stack[-1], Paragraph):
            self._doc_stack.append(None) # Marker for depart_reference to pop
            # Get align because parent may be a figure element
            self._doc_stack.append(self._make_paragraph(
                self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent,
                align=node.parent.get('align')))
        self._doc_stack[-1].begin_hyperlink(
                self._docx.get_style_id('Hyperlink'))

    def depart_reference(self, node):
        refuri = node.get('refuri', None)
        if refuri:
            if node.get('internal', False):
                rid = None
                anchor = self._get_bookmark_name(refuri)
            else:
                rid = self._docx.add_hyperlink_relationship(
                        refuri, self._relationship_stack[-1])
                anchor = None
        else:
            rid = None
            anchor = make_bookmark_name(
                    self._docname_stack[-1], node.get('refid'))
        self._doc_stack[-1].end_hyperlink(rid, anchor)
        if self._doc_stack[-2] is None:
            del self._doc_stack[-2]
            self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_footnote_reference(self, node):
        self._append_bookmark_start(node.get('ids', []))
        refid = node.get('refid', None)
        if refid is not None:
            fid = self._docx.set_default_footnote_id(
                    '%s#%s' % (self._docname_stack[-1], refid))
            self._doc_stack[-1].add_footnote_reference(
                    fid, self._docx.get_style_id('Footnote Reference'))
        self._append_bookmark_end(node.get('ids', []))
        raise nodes.SkipNode

    def visit_citation_reference(self, node):
        self._append_bookmark_start(node.get('ids', []))
        pass # TODO

    def depart_citation_reference(self, node):
        self._append_bookmark_end(node.get('ids', []))
        pass

    def visit_substitution_reference(self, node):
        self._append_bookmark_start(node.get('ids', []))
        pass # TODO

    def depart_substitution_reference(self, node):
        self._append_bookmark_end(node.get('ids', []))
        pass

    def visit_title_reference(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Title Reference')

    def depart_title_reference(self, node):
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_abbreviation(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Abbreviation') # TODO

    def depart_abbreviation(self, node):
        self._doc_stack[-1].pop_style()
        explanation = node.get('explanation')
        if explanation:
            self._doc_stack[-1].add_text(' (%s)' % explanation)
        self._append_bookmark_end(node.get('ids', []))

    def visit_acronym(self, node):
        self._append_bookmark_start(node.get('ids', []))
        pass # TODO

    def depart_acronym(self, node):
        self._append_bookmark_end(node.get('ids', []))
        pass

    def visit_subscript(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Subscript')

    def depart_subscript(self, node):
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_superscript(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Superscript')

    def depart_superscript(self, node):
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_inline(self, node):
        self._append_bookmark_start(node.get('ids', []))
        if 'versionmodified' in node.get('classes'):
            self._push_style('Versionmodified', 'Emphasis')

    def depart_inline(self, node):
        self._append_bookmark_end(node.get('ids', []))
        if 'versionmodified' in node.get('classes'):
            self._doc_stack[-1].pop_style()

    def visit_problematic(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Problematic')

    def depart_problematic(self, node):
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_generated(self, node):
        self._append_bookmark_start(node.get('ids', []))

    def depart_generated(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_target(self, node):
        self._append_bookmark_start(node.get('ids', []))
        pass # TODO

    def depart_target(self, node):
        self._append_bookmark_end(node.get('ids', []))
        pass

    def visit_image(self, node):
        def get_filepath(self, node):
            uri = node['uri']
            if uri.find('://') != -1:
                raise RuntimeError('Not support remote image files yet')
            filepath = os.path.join(self._builder.srcdir, uri)
            if not os.path.exists(filepath):
                # Some extensions output images in imagedir
                filepath = os.path.join(
                        self._builder.outdir, self._builder.imagedir, uri)
            if not os.path.exists(filepath):
                # Some extensions output images in outdir
                filepath = os.path.join(self._builder.outdir, uri)
            return filepath
        self.visit_image_node(
                node, node.get('alt', node['uri']), get_filepath)

    def visit_raw(self, node):
        raise nodes.SkipNode # TODO


    def visit_toctree(self, node):
        if node.get('hidden', False):
            return
        caption = node.get('caption')
        maxdepth = node.get('maxdepth', -1)
        maxlevel = self._section_level + maxdepth if maxdepth > 0 else None
        refid = node.get('docx_expanded_toctree_refid')
        if refid is None:
            self._logger.warning(
                    'No docx_expanded_toctree_refid', location=node)
            return
        bookmark = make_bookmark_name(self._docname_stack[-1], refid)
        self._doc_stack[-1].append(TOC(
            caption, self._docx.get_style_id('TOC Heading'),
            maxlevel, bookmark, self._ctx_stack[-1].paragraph_width,
            self._collect_outlines(node, maxdepth)))
        config = self._builder.config
        if (self._section_level <= config.docx_pagebreak_after_table_of_contents
                and isinstance(self._doc_stack[-1], Document)):
            self._doc_stack[-1].add_pagebreak()

    def depart_toctree(self, node):
        pass

    def visit_compact_paragraph(self, node):
        self._append_bookmark_start(node.get('ids', []))

    def depart_compact_paragraph(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_literal_emphasis(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Literal')
        self._push_style('Emphasis')

    def depart_literal_emphasis(self, node):
        self._doc_stack[-1].pop_style()
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_literal_strong(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Literal')
        self._push_style('Strong')

    def depart_literal_strong(self, node):
        self._doc_stack[-1].pop_style()
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_highlightlang(self, node):
        self._language = node.get('lang', 'guess')
        self._linenothreshold = node.get(
                'linenothreshold', self._linenothreshold)
        raise nodes.SkipNode

    def visit_glossary(self, node):
        self._append_bookmark_start(node.get('ids', []))

    def depart_glossary(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_desc(self, node):
        self._append_bookmark_start(node.get('ids', []))
        table_width = self._ctx_stack[-1].paragraph_width - 500
        style_name = '%s Descriptions' % node.get('desctype', '').capitalize()
        self._docx.create_style(
                'table', style_name, 'Admonition Descriptions', True)
        table = self._append_table(
                style_name, table_width, [1.0], True, fit_content=False)
        table.start_head()
        table.add_row()
        self._add_table_cell()

    def depart_desc(self, node):
        self._pop_and_append_table()
        self._append_bookmark_end(node.get('ids', []))

    def visit_desc_signature(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack.append(self._make_paragraph())

    def depart_desc_signature(self, node):
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_desc_signature_line(self, node):
        self._append_bookmark_start(node.get('ids', []))
        parent = node.parent
        first_option_index = parent.first_child_matching_class(node.__class__)
        if parent[first_option_index] is not node:
            self._doc_stack[-1].add_break()

    def depart_desc_signature_line(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_desc_name(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Desc Name', 'Strong')

    def depart_desc_name(self, node):
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_desc_addname(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Desc Name', 'Strong')

    def depart_desc_addname(self, node):
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_desc_type(self, node):
        self._append_bookmark_start(node.get('ids', []))

    def depart_desc_type(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_desc_returns(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack[-1].add_text(u' → ')

    def depart_desc_returns(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_desc_parameterlist(self, node):
        self._doc_stack[-1].add_text('(')
        self._append_bookmark_start(node.get('ids', []))

    def depart_desc_parameterlist(self, node):
        self._doc_stack[-1].add_text(')')
        self._append_bookmark_end(node.get('ids', []))

    def visit_desc_parameter(self, node):
        self._append_bookmark_start(node.get('ids', []))
        parent = node.parent
        if parent.children[0] is not node:
            self._doc_stack[-1].add_text(parent.child_text_separator)
        if not node.get('noemph', False):
            self._push_style('Emphasis')

    def depart_desc_parameter(self, node):
        if not node.get('noemph', False):
            self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_desc_optional(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack[-1].add_text('[')
        parent = node.parent
        if parent.children[0] is not node:
            self._doc_stack[-1].add_text(parent.child_text_separator)

    def depart_desc_optional(self, node):
        self._doc_stack[-1].add_text(']')
        self._append_bookmark_end(node.get('ids', []))

    def visit_desc_annotation(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._push_style('Desc Annotation', 'Emphasis')

    def depart_desc_annotation(self, node):
        self._doc_stack[-1].pop_style()
        self._append_bookmark_end(node.get('ids', []))

    def visit_desc_content(self, node):
        self._append_bookmark_start(node.get('ids', []))
        if len(node) == 0:
            return
        table = self._doc_stack[-1]
        table.start_body()
        table.add_row()
        self._add_table_cell()

    def depart_desc_content(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_productionlist(self, node):
        raise nodes.SkipNode # TODO

    def depart_productionlist(self, node):
        pass

    def visit_seealso(self, node):
        self.visit_admonition_node(node, add_title=True)

    def depart_seealso(self, node):
        self.depart_admonition_node(node)

    def visit_tabular_col_spec(self, node):
        raise nodes.SkipNode # Do nothing

    def visit_acks(self, node):
        raise nodes.SkipNode # TODO

    def depart_acks(self, node):
        pass

    def visit_centered(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._doc_stack.append(self._make_paragraph(
            self._ctx_stack[-1].indent, self._ctx_stack[-1].right_indent,
            align='center'))

    def depart_centered(self, node):
        self._pop_and_append()
        self._append_bookmark_end(node.get('ids', []))

    def visit_hlist(self, node):
        self._append_bookmark_start(node.get('ids', []))
        table_width = self._ctx_stack[-1].paragraph_width
        numcols = len(node)
        colsize_list = [1.0 / numcols for _ in range(numcols)]
        t = self._append_table(
                None, table_width, colsize_list, True, fit_content=False)
        t.add_row()

    def depart_hlist(self, node):
        self._pop_and_append_table()
        self._append_bookmark_end(node.get('ids', []))

    def visit_hlistcol(self, node):
        self._append_bookmark_start(node.get('ids', []))
        self._add_table_cell()

    def depart_hlistcol(self, node):
        self._append_bookmark_end(node.get('ids', []))

    def visit_versionmodified(self, node):
        self.visit_admonition_node(node)

    def depart_versionmodified(self, node):
        style_name = 'Admonition ' + node.get('type').capitalize()
        self._docx.create_style(
                'table', style_name, 'Admonition Versionmodified', True)
        self.depart_admonition_node(
                node, style=style_name, align=None, margin=0)

    def visit_index(self, node):
        self._append_bookmark_start(node.get('ids', []))
        pass # TODO

    def depart_index(self, node):
        self._append_bookmark_end(node.get('ids', []))
        pass

    def visit_pending_xref(self, node):
        self._append_bookmark_start(node.get('ids', []))
        pass # TODO

    def depart_pending_xref(self, node):
        self._append_bookmark_end(node.get('ids', []))
        pass

    def visit_download_reference(self, node):
        self._append_bookmark_start(node.get('ids', []))
        pass # TODO

    def depart_download_reference(self, node):
        self._append_bookmark_end(node.get('ids', []))
        pass

    def visit_number_reference(self, node):
        self.visit_reference(node)

    def depart_number_reference(self, node):
        self.depart_reference(node)

    def visit_meta(self, node):
        raise nodes.SkipNode

    def visit_graphviz(self, node):
        def get_filepath(self, node):
            fname, filepath = graphviz.render_dot(
                self, node['code'], node['options'], 'png')
            if filepath is None:
                raise RuntimeError('Failed to generate a graphviz image')
            return filepath
        self.visit_image_node(
                node, node.get('alt', (node['code'], 'dot')), get_filepath)

    def visit_refcount(self, node):
        raise nodes.SkipNode # TODO

    def depart_refcount(self, node):
        pass

    def visit_displaymath(self, node):
        self.visit_math_block_node(node, node.get('latex'))

    def visit_todo_node(self, node):
        self.visit_admonition_node(node)

    def depart_todo_node(self, node):
        self.depart_admonition_node(node)

    def unknown_visit(self, node):
        self._logger.warning(
                'Ignore unknown node ' + node.tagname, location=node)
        raise nodes.SkipNode

    def _get_bookmark_name(self, refuri):
        # For such case that the target is in a different directory
        refuri = os.path.normpath(
                os.path.join(os.path.dirname(self._docname_stack[-1]), refuri))
        if refuri in self._builder.env.all_docs:
            return make_bookmark_name(refuri, '')
        hashindex = refuri.rfind('#') # Use rfind because docname includes #.
        if hashindex != -1 and refuri[:hashindex] in self._builder.env.all_docs:
            return make_bookmark_name(refuri[:hashindex], refuri[hashindex+1:])
        if hashindex == 0:
            return make_bookmark_name(self._docname_stack[-1], refuri[1:])
        return None

    def _get_additional_list_indent(self, list_level):
        if list_level >= len(self._bullet_list_indents):
            return self._basic_indent
        if list_level == 0:
            parent_indent = 0
        else:
            parent_indent = self._bullet_list_indents[list_level - 1]
        return self._bullet_list_indents[list_level] - parent_indent

    def _get_image_scaled_size(self, node, filename):
        paragraph_width = self._ctx_stack[-1].paragraph_width
        width = self._get_cm_size(node, 'width', paragraph_width)
        height = self._get_cm_size(node, 'height')

        if width is None and height is None:
            width, height = get_image_size(filename)
        elif width is None:
            img_width, img_height = get_image_size(filename)
            width = img_width * height / img_height
        elif height is None:
            img_width, img_height = get_image_size(filename)
            height = img_height * width / img_width

        scale = node.get('scale')
        if scale is not None:
            scale = float(scale) / 100
            width *= scale
            height *= scale

        width, height = adjust_size(
                convert_to_cm_size(paragraph_width), width, height)
        # 600 is margin for caption
        max_height = self._doc_stack[0].get_current_page_height() - 600
        height, width = adjust_size(
                convert_to_cm_size(max_height), height, width)

        return width, height

    def _get_cm_size(self, node, attr, max_width=0):
        try:
            return convert_to_cm_size(
                    convert_to_twip_size(node.get(attr), max_width))
        except Exception as e:
            self._logger.warning(e, location=node)
            return None

    def _collect_outlines(self, node, maxdepth):
        toctree = TocTree(self._builder.env).resolve(
                self._docname_stack[-1], self._builder, node,
                maxdepth=maxdepth, includehidden=True)
        if toctree is None:
            return []
        outlines = []
        for outline in toctree.traverse(
                addnodes.compact_paragraph, include_self=False):
            classes = outline.get('classes')
            level_class = next(c for c in classes if c.startswith('toctree-l'))
            ref = outline[0]
            secnum = ref.get('secnumber')
            if secnum is not None:
                text = '.'.join(map(str, secnum)) + ' ' + ref.astext()
            else:
                text = ref.astext()
            outlines.append((
                text,
                self._docx.get_style_id(
                    level_class.replace('toctree-l', 'toc ')),
                self._get_bookmark_name(ref.get('refuri'))))
        return outlines

    def _create_docxbuilder_styles(self):
        self._docx.create_empty_paragraph_style('Transition', 100, True, False)
        self._docx.create_empty_paragraph_style(
                'Table Bottom Margin', 0, False, True)

        default_pargraph, _, default_table = self._docx.get_default_style_names()
        paragraph_styles = [
                ('Body Text', default_pargraph, False, False),
                ('Footnote Text', default_pargraph, False, False),
                ('Bibliography', default_pargraph, False, False),
                ('Definition Term', default_pargraph, True, False),
                ('Literal Block', default_pargraph, True, False),
                ('Math Block', default_pargraph, True, False),
                ('Figure', default_pargraph, True, False),
                ('Caption', default_pargraph, False, True),
                ('Table Caption', 'Caption', True, False),
                ('Image Caption', 'Caption', True, False),
                ('Literal Caption', 'Caption', True, False),
                ('Heading', default_pargraph, True, True),
                ('Title Heading', 'Heading', True, True),
                ('TOC Heading', 'Title Heading', False, False),
                ('Rubric Title Heading', 'Title Heading', True, False),
                ('Subtitle Heading', 'Heading', True, True),
        ]
        for new_style, based_style, is_custom, is_hidden in paragraph_styles:
            self._docx.create_style(
                    'paragraph', new_style, based_style, is_custom, is_hidden)

        self._docx.create_list_style(
                'List Bullet', 'bullet', '\uf0b7', 'Symbol', self._basic_indent)
        self._docx.create_list_style(
                'List Number', 'arabic', '%1.', None, self._basic_indent)

        table_styles = [
                ('List Table', default_table, False, True),
                ('Table', default_table, False, False),
                ('Based Admonition', default_table, False, True),
                ('Field List', 'List Table', False, False),
                ('Option List', 'List Table', False, False),
                ('Admonition', 'Based Admonition', False, False),
                ('Admonition Descriptions', 'Based Admonition', False, True),
                ('Admonition Versionmodified', 'Based Admonition', True, True),
        ]
        for new_style, based_style, is_custom, is_hidden in table_styles:
            self._docx.create_style(
                    'table', new_style, based_style, is_custom, is_hidden)
