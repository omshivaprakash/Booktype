"""
Some common functions for booki editor.
"""
import tempfile
import urllib2
from urllib import urlencode
import zipfile
import os, sys
import datetime
import re
import logging
from cStringIO import StringIO
import traceback
import time

try:
    import simplejson as json
except ImportError:
    import json

from lxml import etree, html

from django import template
from django.template.defaultfilters import slugify
from django.utils.translation import ugettext_lazy as _

from booki.editor import models
from booki.bookizip import get_metadata, add_metadata, DC, FM

from booki.utils.log import logBookHistory, logWarning
from booki.utils.book import createBook
from booki.editor.views import getVersion


try:
    from booki.settings import THIS_BOOKI_SERVER, DEFAULT_PUBLISHER
except ImportError:
    THIS_BOOKI_SERVER = os.environ.get('HTTP_HOST', 'www.booki.cc')
    DEFAULT_PUBLISHER = "FLOSS Manuals http://flossmanuals.net"


class BookiError(Exception):
    pass

# parse JSON

def parseJSON(js):
    try:
        return json.loads(js)
    except Exception:
        return {}


def makeTitleUnique(requestedTitle):
    """If there is called <requestedTitle>, return that. Otherwise,
    return a title in the form `u'%s - %d' % (requestedTitle, n)`
    where n is the lowest non-clashing positive integer.
    """
    n = 0
    name = requestedTitle
    while True:
        try:
            book = models.Book.objects.get(title=name)
            n += 1
            name = u'%s - %d' % (requestedTitle, n)
        except:
            break
    return name


def getChaptersFromTOC(toc):
    """Convert a nested bookizip TOC structure into a list of tuples
    in the form:

    (title, url, is_this_chapter_really_a_booki_section?)
    """
    chapters = []
    for elem in toc:
        chapters.append((elem.get('title', 'Missing title'),
                         elem.get('url', 'Missing URL'),
                         elem.get('type', 'chapter') == 'booki-section'))
        if elem.get('children'):
            chapters.extend(getChaptersFromTOC(elem['children']))

    return chapters


def importBookFromFile(user, zname, createTOC=False):
    """Create a new book from a bookizip filename"""
    # unzip it
    zf = zipfile.ZipFile(zname)
    # load info.json
    info = json.loads(zf.read('info.json'))
    logWarning("Loaded json file %r" % info)

    metadata = info['metadata']
    manifest = info['manifest']
    TOC =      info['TOC']

    bookTitle = get_metadata(metadata, 'title', ns=DC)[0]
    bookTitle = makeTitleUnique(bookTitle)

    book = createBook(user, bookTitle, status = "imported")

    # this is for Table of Contents
    p = re.compile('\ssrc="(.*)"')

    # what if it does not have status "imported"
    stat = models.BookStatus.objects.filter(book=book, name="imported")[0]

    chapters = getChaptersFromTOC(TOC)
    n = len(chapters) + 1 #is +1 necessary?
    now = datetime.datetime.now()

    for chapterName, chapterFile, is_section in chapters:
        urlName = slugify(chapterName)

        if is_section: # create section
            if createTOC:
                c = models.BookToc(book = book,
                                   version = book.version,
                                   name = chapterName,
                                   chapter = None,
                                   weight = n,
                                   typeof = 2)
                c.save()
                n -= 1
        else: # create chapter
            # check if i can open this file at all
            content = zf.read(chapterFile)

            content = p.sub(r' src="../\1"', content)

            chapter = models.Chapter(book = book,
                                     version = book.version,
                                     url_title = urlName,
                                     title = chapterName,
                                     status = stat,
                                     content = content,
                                     created = now,
                                     modified = now)
            chapter.save()

            if createTOC:
                c = models.BookToc(book = book,
                                   version = book.version,
                                   name = chapterName,
                                   chapter = chapter,
                                   weight = n,
                                   typeof = 1)
                c.save()
                n -= 1

    stat = models.BookStatus.objects.filter(book=book, name="imported")[0]

    from django.core.files import File

    for item in manifest.values():
        if item["mimetype"] != 'text/html':
            attachmentName = item['url']

            if attachmentName.startswith("static/"):
                att = models.Attachment(book = book,
                                        version = book.version,
                                        status = stat)

                s = zf.read(attachmentName)
                f = StringIO(s)
                f2 = File(f)
                f2.size = len(s)
                att.attachment.save(os.path.basename(attachmentName), f2, save=False)
                att.save()
                f.close()

    # metadata
    for namespace in metadata:
        # namespace is something like "http://purl.org/dc/elements/1.1/" or ""
        # in the former case, preepend it to the name, in {}.
        ns = ('{%s}' % namespace if namespace else '')
        for keyword, schemes in metadata[namespace].iteritems():
            for scheme, values in schemes.iteritems():
                #schema, if it is set, describes the value's format.
                #for example, an identifier might be an ISBN.
                sc = ('{%s}' % scheme if scheme else '')
                key = "%s%s%s" % (ns, keyword, sc)
                for v in values:
                    info = models.Info(book=book, name=key)
                    if len(v) >= 2500:
                        info.value_text = v
                        info.kind = 2
                    else:
                        info.value_string = v
                        info.kind = 0
                    info.save()
    zf.close()






def importBookFromURL(user, bookURL, createTOC=False):
    """
    Imports book from the url. Creates project and book for it.
    """
    # download it
    try:
        f = urllib2.urlopen(bookURL)
        data = f.read()
        f.close()
    except urllib2.URLError, e:
        logWarning("couldn't read %r: %s" % (bookURL, e))
        logWarning(traceback.format_exc())
        raise

    try:
        zf = StringIO(data)
        importBookFromFile(user, zf, createTOC)
        zf.close()
    except Exception, e:
        logWarning("couldn't make book from %r: %s" % (bookURL, e))
        logWarning(traceback.format_exc())
        raise


def importBookFromUrl2(user, baseurl, **args):
    args['mode'] = 'zip'
    url = baseurl + "?" + urlencode(args)
    importBookFromURL(user, url, createTOC=True)



def expand_authors(book, chapter, content):
    t = template.loader.get_template_from_string('{% load booki_tags %} {% booki_authors book %}')
    con = t.render(template.Context({"content": chapter, "book": book}))
    return content.replace('##AUTHORS##', con)



def _format_metadata(book):
    metadata = {}
    # there must be language, creator, identifier and title
    #key is [ '{' namespace '}' ] name [ '[' scheme ']' ]
    key_re = re.compile(r'^(?:\{([^}]*)\})?'  # namespace
                        r'(.+)'              # keyword
                        r'(?:\[([^}]*)\])?$'  #schema
                        )

    for item in models.Info.objects.filter(book=book):
        key = item.name
        value = item.getValue()
        m = key_re.match(key)
        if m is None:
            keyword = key
            namespace, scheme = '', ''
        else:
            namespace, keyword, scheme = m.groups('')
        add_metadata(metadata, keyword, value, namespace, scheme)

    now = time.strftime("%Y.%m.%d-%H.%M")
    created = book.created.strftime("%Y.%m.%d-%H.%M")
    lastmod = (models.BookHistory.objects.filter(book=book)
               .dates("modified", "day", order='DESC')[0]
               .strftime("%Y.%m.%d-%H.%M"))

    # add some default values if values are not otherwise specified
    for namespace, keyword, scheme, value in (
        (DC, "publisher", "", DEFAULT_PUBLISHER),
        (DC, "language", "", "en"),
        (DC, "creator", "", "The Contributors"),
        (DC, "title", "", book.title),
        (DC, "date", "start", created),
        (DC, "date", "last-modified", lastmod),
        (DC, "date", "published", now),
        (DC, "identifier", "booki.cc", "http://%s/%s/%s" % (THIS_BOOKI_SERVER, book.url_title, now))
        ):
        if not get_metadata(metadata, keyword, namespace, scheme):
            add_metadata(metadata, keyword, value, namespace, scheme)

    #XXX add contributors
    return metadata


def _fix_content(book, chapter):
    """fix up the html in various ways"""
    content = chapter.chapter.content
    if not content:
        return '<body><!--no content!--></body>'

    #As a special case, the ##AUTHORS## magic string gets expanded into the authors list.
    if "##AUTHORS##" in content:
        expand_authors(book, chapter, content)

    if 0:
        #for timing comparison
        p = re.compile('\ssrc="\.\.\/(.*)"')
        p2 = re.compile('\ssrc=\"\/[^\"]+\/([^"]+)\"')
        import htmlentitydefs
        exclude = ['quot', 'amp', 'apos', 'lt', 'gt']
        content = p.sub(r' src="\1"', content)
        content = p2.sub(r' src="static/\1"', content)
        for ky, val in htmlentitydefs.name2codepoint.items():
            if ky not in exclude:
                content = content.replace(unichr(val), '&%s;' % (ky, ))
        if isinstance(content, unicode):
            content = content.encode('utf-8')
        return content

    if isinstance(content, unicode):
        content = content.encode('utf-8')

    tree = html.document_fromstring(content)

    base = "/%s/" % (book.url_title,)
    here = base + chapter.chapter.url_title
    from os.path import join, normpath
    from urlparse import urlsplit, urlunsplit

    def flatten(url, prefix):
        scheme, addr, path, query, frag = urlsplit(url)
        if scheme: #http, ftp, etc, ... ignore it
            return url
        path = normpath(join(here, path))
        if not path.startswith(base + prefix):
            #What is best here? make an absolute http:// link?
            #for now, ignore it.
            logWarning("got a wierd link: %r in %s resolves to %r, wanted start of %s" %
                (url, here, path, base + prefix))
            return url
        path = path[len(base):]
        logWarning("turning %r into %r" % (url, path))
        return urlunsplit(('', '', path, query, frag))

    for e in tree.iter():
        src = e.get('src')
        if src is not None:
            # src attributes that point to '../static', should point to 'static'
            e.set('src', flatten(src, 'static'))

        href = e.get('href')
        if href is not None:
            e.set('href', flatten(href, ''))

    return content




def exportBook(book_version):
    from booki import bookizip
    import time
    starttime = time.time()

    (zfile, zname) = tempfile.mkstemp()

    spine = []
    toc_top = []
    toc_current = toc_top
    waiting_for_url = []

    info = {
        "version": 1,
        "TOC": toc_top,
        "spine": spine,
        "metadata": _format_metadata(book_version.book),
        "manifest": {}
        }

    bzip = bookizip.BookiZip(zname, info=info)

    for i, chapter in enumerate(models.BookToc.objects.filter(version=book_version).order_by("-weight")):
        if chapter.chapter:
            # It's a real chapter! With content!
            content = _fix_content(book_version.book, chapter)

            ID = "ch%03d_%s" % (i, chapter.chapter.url_title.encode('utf-8'))
            filename = ID + '.html'

            toc_current.append({"title": chapter.chapter.title,
                                "url": filename,
                                "type": "chapter",
                                "role": "text"
                                })

            # If this is the first chapter in a section, lend our url
            # to the section, which has no content and thus no url of
            # its own.  If this section was preceded by an empty
            # section, it will be waiting too, hence "while" rather
            # than "if".
            while waiting_for_url:
                section = waiting_for_url.pop()
                section["url"] = filename

            bzip.add_to_package(ID, filename, content, "text/html")
            spine.append(ID)

        else:
            #A new top level section.
            title = chapter.name.encode("utf-8")
            ID = "s%03d_%s" % (i, slugify(title))

            toc_current = []
            section = {"title": title,
                       "url": '',
                       "type": "booki-section",
                       "children": toc_current
                       }

            toc_top.append(section)
            waiting_for_url.append(section)


    #Attachments are images (and perhaps more).  They do not know
    #whether they are currently in use, or what chapter they belong
    #to, so we add them all.
    #XXX scan for img links while adding chapters, and only add those.

    for i, attachment in enumerate(models.Attachment.objects.filter(version=book_version)):
        try:
            f = open(attachment.attachment.name, "rb")
            blob = f.read()
            f.close()
        except (IOError, OSError), e:
            msg = "couldn't read attachment %s" % e
            logWarning(msg)
            continue

        fn = os.path.basename(attachment.attachment.name.encode("utf-8"))

        ID = "att%03d_%s" % (i, fn)
        if '.' in ID:
            ID, ext = ID.rsplit('.', 1)
            mediatype = bookizip.MEDIATYPES[ext.lower()]
        else:
            mediatype = bookizip.MEDIATYPES[None]

        bzip.add_to_package(ID,
                            "static/%s" % fn,
                            blob,
                            mediatype)


    bzip.finish()
    logWarning("export took %s seconds" % (time.time() - starttime))
    return zname
