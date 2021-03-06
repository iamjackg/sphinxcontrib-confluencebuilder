# -*- coding: utf-8 -*-
"""
    sphinxcontrib.confluencebuilder.builder
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    :copyright: Copyright 2016-2017 by the contributors (see AUTHORS file).
    :license: BSD, see LICENSE.txt for details.
"""

from __future__ import (print_function, unicode_literals, absolute_import)
from .config import ConfluenceConfig
from .compat import ConfluenceCompat
from .exceptions import ConfluenceConfigurationError
from .logger import ConfluenceLogger
from .publisher import ConfluencePublisher
from .state import ConfluenceState
from .writer import ConfluenceWriter
from docutils.io import StringOutput
from docutils import nodes
from sphinx.builders import Builder
from sphinx.util.osutil import ensuredir, SEP
from sphinx import addnodes
from os import path
import io

# Clone of relative_uri() sphinx.util.osutil, with bug-fixes
# since the original code had a few errors.
# This was fixed in Sphinx 1.2b.
def relative_uri(base, to):
    """Return a relative URL from ``base`` to ``to``."""
    if to.startswith(SEP):
        return to
    b2 = base.split(SEP)
    t2 = to.split(SEP)
    # remove common segments (except the last segment)
    for x, y in zip(b2[:-1], t2[:-1]):
        if x != y:
            break
        b2.pop(0)
        t2.pop(0)
    if b2 == t2:
        # Special case: relative_uri('f/index.html','f/index.html')
        # returns '', not 'index.html'
        return ''
    if len(b2) == 1 and t2 == ['']:
        # Special case: relative_uri('f/index.html','f/') should
        # return './', not ''
        return '.' + SEP
    return ('..' + SEP) * (len(b2)-1) + SEP.join(t2)

class ConfluenceBuilder(Builder):
    cache_doctrees = {}
    current_docname = None
    omitted_docnames = []
    publish_docnames = []
    name = 'confluence'
    format = 'confluence'
    file_suffix = '.conf'
    link_suffix = None  # defaults to file_suffix
    master_doc_page_id = None
    publisher = ConfluencePublisher()

    def init(self, suppress_conf_check=True):
        if not ConfluenceConfig.validate(self.config, not suppress_conf_check):
            raise ConfluenceConfigurationError('configuration error')

        self.writer = ConfluenceWriter(self)
        self.publisher.init(self.config)

        server_url = self.config.confluence_server_url
        if server_url and server_url.endswith('/'):
            self.config.confluence_server_url = server_url[:-1]

        if self.config.confluence_file_suffix is not None:
            self.file_suffix = self.config.confluence_file_suffix
        if self.config.confluence_link_suffix is not None:
            self.link_suffix = self.config.confluence_link_suffix
        elif self.link_suffix is None:
            self.link_suffix = self.file_suffix

        # Function to convert the docname to a reST file name.
        def file_transform(docname):
            return docname + self.file_suffix

        # Function to convert the docname to a relative URI.
        def link_transform(docname):
            return docname + self.link_suffix

        if self.config.confluence_file_transform is not None:
            self.file_transform = self.config.confluence_file_transform
        else:
            self.file_transform = file_transform
        if self.config.confluence_link_transform is not None:
            self.link_transform = self.config.confluence_link_transform
        else:
            self.link_transform = link_transform

        if self.config.confluence_publish:
            self.publish = True
            self.publisher.connect()
            self.parent_id = self.publisher.getBasePageId()
            self.legacy_pages = self.publisher.getDescendents(self.parent_id)
        else:
            self.publish = False
            self.parent_id = None
            self.legacy_pages = []

        if self.config.confluence_space_name is not None:
            self.space_name = self.config.confluence_space_name
        else:
            self.space_name = None

    def get_outdated_docs(self):
        """
        Return an iterable of input files that are outdated.
        """
        # This method is taken from TextBuilder.get_outdated_docs()
        # with minor changes to support :confval:`rst_file_transform`.
        for docname in self.env.found_docs:
            if docname not in self.env.all_docs:
                yield docname
                continue
            sourcename = path.join(self.env.srcdir, docname +
                                   self.file_suffix)
            targetname = path.join(self.outdir, self.file_transform(docname))
            print (sourcename, targetname)

            try:
                targetmtime = path.getmtime(targetname)
            except Exception:
                targetmtime = 0
            try:
                srcmtime = path.getmtime(sourcename)
                if srcmtime > targetmtime:
                    yield docname
            except EnvironmentError:
                # source doesn't exist anymore
                pass

    def get_target_uri(self, docname, typ=None):
        return self.link_transform(docname)

    def get_relative_uri(self, from_, to, typ=None):
        """
        Return a relative URI between two source filenames.
        """
        # This is slightly different from Builder.get_relative_uri,
        # as it contains a small bug (which was fixed in Sphinx 1.2).
        return relative_uri(self.get_target_uri(from_),
                            self.get_target_uri(to, typ))

    def prepare_writing(self, docnames):
        ordered_docnames = []
        traversed = [self.config.master_doc]

        # prepare caching doctree hook
        #
        # We'll temporarily override the environment's 'get_doctree' method to
        # allow this extension to manipulate the doctree for a document inside
        # the pre-writing stage to also take effect in the writing stage.
        self._original_get_doctree = self.env.get_doctree
        self.env.get_doctree = self._get_doctree

        # process the document structure of the master document, allowing:
        #  - populating a publish order to ensure parent pages are created first
        #     (when using hierarchy mode)
        #  - squash pages which exceed maximum depth (if configured with a max
        #     depth value)
        self.process_tree_structure(
            ordered_docnames, self.config.master_doc, traversed)

        # add orphans (if any) to the publish list
        ordered_docnames.extend(x for x in docnames if x not in traversed)

        for docname in docnames:
            doctree = self.env.get_doctree(docname)

            # find title for document
            idx = doctree.first_child_matching_class(nodes.section)
            if idx is None or idx == -1:
                continue

            first_section = doctree[idx]
            idx = first_section.first_child_matching_class(nodes.title)
            if idx is None or idx == -1:
                continue

            doctitle = first_section[idx].astext()
            if not doctitle:
                if self.publish:
                    ConfluenceLogger.warn("document will not be published "
                        "since it has no title: %s" % docname)
                continue

            doctitle = ConfluenceState.registerTitle(docname, doctitle,
                self.config.confluence_publish_prefix)
            if docname in ordered_docnames:
                self.publish_docnames.append(docname)

            target_refs = []
            for node in doctree.traverse(nodes.target):
                if 'refid' in node:
                    target_refs.append(node['refid'])

            doc_used_names = {}
            for node in doctree.traverse(nodes.title):
                if isinstance(node.parent, nodes.section):
                    section_node = node.parent
                    if 'ids' in section_node:
                        target = ''.join(node.astext().split())
                        section_id = doc_used_names.get(target, 0)
                        doc_used_names[target] = section_id + 1
                        if section_id > 0:
                            target = '%s.%d' % (target, section_id)

                        for id in section_node['ids']:
                            if not id in target_refs:
                                id = '%s#%s' % (docname, id)
                            ConfluenceState.registerTarget(id, target)

        ConfluenceState.titleConflictCheck()

    def process_tree_structure(self, ordered, docname, traversed, depth=0):
        omit = False
        max_depth = self.config.confluence_max_doc_depth
        if max_depth is not None and depth > max_depth:
            omit = True
            self.omitted_docnames.append(docname)

        if not omit:
            ordered.append(docname)

        modified = False
        doctree = self.env.get_doctree(docname)
        for toctreenode in doctree.traverse(addnodes.toctree):
            if not omit and max_depth is not None:
                if (depth + toctreenode['maxdepth']) > max_depth:
                    new_depth = max_depth - depth
                    assert new_depth >= 0
                    toctreenode['maxdepth'] = new_depth
            movednodes = []
            for child in toctreenode['includefiles']:
                if child not in traversed:
                    ConfluenceState.registerParentDocname(child, docname)
                    traversed.append(child)

                    children = self.process_tree_structure(
                        ordered, child, traversed, depth+1)
                    if children:
                        movednodes.append(children)
                        self._fix_std_labels(child, docname)

            if movednodes:
                modified = True
                toctreenode.replace_self(movednodes)
                toctreenode.parent['classes'].remove('toctree-wrapper')

        if omit:
            container = addnodes.start_of_file(docname=docname)
            container.children = doctree.children
            return container
        elif modified:
            self.env.resolve_references(doctree, docname, self)

    def write_doc(self, docname, doctree):
        if docname in self.omitted_docnames:
            return
        self.current_docname = docname

        # remove title from page contents
        if self.config.confluence_remove_title:
            idx = doctree.first_child_matching_class(nodes.section)
            if not idx == None and not idx == -1:
                first_section = doctree[idx]
                idx = first_section.first_child_matching_class(nodes.title)
                if not idx == None and not idx == -1:
                    doctitle = first_section[idx].astext()
                    if doctitle:
                        first_section.remove(first_section[idx])

        # This method is taken from TextBuilder.write_doc()
        # with minor changes to support :confval:`rst_file_transform`.
        destination = StringOutput(encoding='utf-8')

        self.writer.write(doctree, destination)
        outfilename = path.join(self.outdir, self.file_transform(docname))
        if self.writer.output:
            ensuredir(path.dirname(outfilename))
            try:
                with io.open(outfilename, 'w', encoding='utf-8') as file:
                    file.write(self.writer.output)
            except (IOError, OSError) as err:
                ConfluenceLogger.warn("error writing file "
                    "%s: %s" % (outfilename, err))

    def publish_doc(self, docname, output):
        title = ConfluenceState.title(docname)

        parent_id = None
        if self.config.master_doc and self.config.confluence_page_hierarchy:
            if self.config.master_doc != docname:
                parent = ConfluenceState.parentDocname(docname)
                parent_id = ConfluenceState.uploadId(parent)
        if not parent_id:
            parent_id = self.parent_id

        uploaded_id = self.publisher.storePage(title, output, parent_id)
        ConfluenceState.registerUploadId(docname, uploaded_id)

        if self.config.master_doc == docname:
            self.master_doc_page_id = uploaded_id

        if self.config.confluence_purge:
            if uploaded_id in self.legacy_pages:
                self.legacy_pages.remove(uploaded_id)

    def publish_finalize(self):
        if self.master_doc_page_id:
            if self.config.confluence_master_homepage is True:
                ConfluenceLogger.info('updating space\'s homepage... ', nonl=0)
                self.publisher.updateSpaceHome(self.master_doc_page_id)
                ConfluenceLogger.info('done\n')

    def publish_purge(self):
        if self.config.confluence_purge is True and self.legacy_pages:
            ConfluenceLogger.info('removing legacy pages... ', nonl=0)
            for legacy_page_id in self.legacy_pages:
               self.publisher.removePage(legacy_page_id)
            ConfluenceLogger.info('done\n')

    def finish(self):
        self.env.get_doctree = self._original_get_doctree

        if self.publish:
            for docname in ConfluenceCompat.status_iterator(self,
                    self.publish_docnames, 'publishing... ',
                    length=len(self.publish_docnames)):
                docfile = path.join(self.outdir, self.file_transform(docname))

                try:
                    with io.open(docfile, 'r', encoding='utf-8') as file:
                        output = file.read()
                        self.publish_doc(docname, output)

                except (IOError, OSError) as err:
                    ConfluenceLogger.warn("error reading file %s: "
                        "%s" % (docfile, err))

            self.publish_purge()
            self.publish_finalize()

    def cleanup(self):
        if self.publish:
            self.publisher.disconnect()

    def _fix_std_labels(self, olddocname, newdocname):
        """
        fix standard domain labels for squashed documents

        When Sphinx resolves references for a doctree ('resolve_references'),
        the standard domain's internal labels are used to map references to
        target documents. To support document squashing (aka. max depth pages),
        this utility method helps override a document's tuple labels so that any
        squashed page's labels can be moved into a parent document's label set.
        """
        # see also: sphinx/domains/std.py
        domain = self.env.get_domain('std')
        for key, (fn, _l, lineno) in list(domain.data['citations'].items()):
            if fn == olddocname:
                data = domain.data['citations'][key]
                domain.data['citations'][key] = newdocname, data[1], data[2]
        for key, docnames in list(domain.data['citation_refs'].items()):
            if fn == olddocname:
                data = domain.data['citation_refs'][key]
                domain.data['citation_refs'][key] = newdocname
        for key, (fn, _l, _l) in list(domain.data['labels'].items()):
            if fn == olddocname:
                data = domain.data['labels'][key]
                domain.data['labels'][key] = newdocname, data[1], data[2]
        for key, (fn, _l) in list(domain.data['anonlabels'].items()):
            if fn == olddocname:
                data = domain.data['anonlabels'][key]
                domain.data['anonlabels'][key] = newdocname, data[1]

    def _get_doctree(self, docname):
        """
        override 'get_doctree' method

        To support document squashing (aka. max depth pages), doctree's may be
        loaded and manipulated before the writing stage. Normally, the writing
        stage will load target doctree's from their source so there is no way to
        pre-load and pass a document's doctree into the writing stage. To
        overcome this, this extension hooks into the environment's 'get_doctree'
        method and caches loaded document's doctree's into a map.
        """
        if docname not in self.cache_doctrees:
            self.cache_doctrees[docname] = self._original_get_doctree(docname)
        return self.cache_doctrees[docname]
