# -*- coding: utf-8 -*-

import datetime
import jinja2
import json
import logging
import math
import mimetypes
import os
import random
import re
import urllib
import webapp2

from google.appengine.api import images
from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.api import search
from google.appengine.api import users
from google.appengine.ext import blobstore
from google.appengine.ext import ndb
from google.appengine.ext.db import BadValueError

import lib.cloudstorage as gcs

from Models import Comment, Plaque

ADMIN_EMAIL = 'kester+readtheplaque@gmail.com'
NOTIFICATION_SENDER_EMAIL = ADMIN_EMAIL
ADD_STATE_SUCCESS = 'success'
ADD_STATE_ERROR = 'error'
ADD_STATES = {'ADD_STATE_SUCCESS': ADD_STATE_SUCCESS,
              'ADD_STATE_ERROR': ADD_STATE_ERROR}

# TODO before go-live
# * Search for plaque text and title
# * Change .net to .com for final deploy

# The wordpress admin dashboard is pretty easy to use and the process to
# publish pending the pending plaques is straightforward.
#
# Here is my proposed plan of action:
#
# 0) Disable comments entirely. They appear to be at least 99.9% spam, with
#    over twenty thousand pending comments.
# 1) Publish the ~200 pending plaque posts. This will take a couple months at
#    the level of effort that I can give to this.
# 2) Go through the ~100 plaques on the #readtheplaque hashtag, post those.
# 3) Copy all of the data from the wordpress site to my revamp of the site.
#    Verify that the data is identical.
# 4) Switch the domain to point to the new site.
# 5) Announce a relauch of the site, from both Roman and Alexis


# GCS_BUCKET configuration: This appears to work for the bucket named
# 'read-the-plaque.appspot.com', but it is different from surlyfritter. I
# suspect I did something different/wrong in the setup, but not sure.
#
GCS_BUCKET = '/read-the-plaque.appspot.com'

DEFAULT_PLAQUESET_NAME = 'public'
DEFAULT_PLAQUES_PER_PAGE = 24

# Load templates from the /templates dir
JINJA_ENVIRONMENT = jinja2.Environment (
    loader=jinja2.FileSystemLoader(
        os.path.join(os.path.dirname(__file__),
                     'templates')),
    extensions=['jinja2.ext.autoescape'],
    autoescape=False)#True) # turn off autoescape to allow html redering of descriptions

# Set a parent key on the Plaque objects to ensure that they are all in the
# same entity group. Queries across the single entity group will be consistent.
# However, the write rate should be limited to ~1/second.

def get_default_template_values(**kwargs):
    template_values = memcache.get('default_template_values')
    if template_values is None:
        template_values = {
            'num_pending': Plaque.num_pending(),
            'footer_items': get_footer_items(),
            'loginout': loginout(),
            'pages_list': get_pages_list(),
        }
        memcache_status = memcache.set('default_template_values', template_values)
        if not memcache_status:
            logging.debug("memcaching for default_template_values failed")
    else:
        logging.debug("memcache.get worked for default_template_values")

    for k, v in kwargs.items():
        template_values[k] = v
    return template_values

def email_admin(plaque, comment=None):
    if comment:
        subject='Comment added to plaque {0.title} at {%0.page_url}'.format(plaque)
        msg = """Comment
                 <p>{0.text}</p>
                 added to {1.title}
                 <a href="http://readtheplaque.net{1.page_url}">
                 here</a>""".format(comment, plaque)
    else:
        subject='New Plaque {0.title} added at {0.page_url}'.format(plaque)
        msg = """<p>Plaque '{0.title}'</p>
                 <p>at {0.page_url}</p>
                 <p>{0.description}</p> added
                 <a href="http://readtheplaque.net{0.page_url}">
                 here</a>""".format(plaque)

    mail.send_mail(sender=NOTIFICATION_SENDER_EMAIL,
                   to=ADMIN_EMAIL,
                   subject=subject,
                   body=msg,
                   html=msg)

def plaqueset_key(plaqueset_name=DEFAULT_PLAQUESET_NAME):
    """
    Constructs a Datastore key for a Plaque entity. Use plaqueset_name as
    the key.
    """
    return ndb.Key('Plaque', plaqueset_name)

def last_five_approved(cls):
    new_items = cls.query(cls.approved == True
                  ).order(-cls.created_on
                  ).fetch(limit=5)
    return new_items

def get_pages_list(plaques_per_page=DEFAULT_PLAQUES_PER_PAGE):
    num_pages = int(math.ceil(float(Plaque.num_approved()) /
                              float(plaques_per_page)))
    pages_list = [1+p for p in range(num_pages)]
    return pages_list

def get_footer_items():
    """
    Just 5 tags for the footer.
    Memcache the output of this so it doesn't get calculated every time.
    """
    footer_items = memcache.get('get_footer_items')
    if footer_items is None:
        tags = set()
        for p in Plaque.query(Plaque.approved == True).fetch():
            for t in p.tags:
                tags.add(t)
        while len(tags) > 5:
            tags.pop()

        footer_items = {'tags': tags,
                        'new_plaques': last_five_approved(Plaque),
                        'new_comments': last_five_approved(Comment)}

        memcache_status = memcache.set('get_footer_items', footer_items)
        if not memcache_status:
            logging.debug("memcaching for get_footer_items failed")
    else:
        logging.debug("memcache.get worked for get_footer_items")

    return footer_items

def loginout():
    # Login/Logout link:
    user = users.get_current_user()
    if user:
        loginout = {'is_admin': users.is_current_user_admin(),
                    'url': users.create_logout_url('/'),
                    'text': 'Log out'}
    else:
        loginout = {'is_admin': users.is_current_user_admin(),
                    'url': users.create_login_url('/'),
                    'text': 'Admin login'}
    return loginout


class ViewPlaquesPage(webapp2.RequestHandler):
    def get(self, page_num=1, plaques_per_page=DEFAULT_PLAQUES_PER_PAGE):
        """
        View the nth plaques_per_page plaques on a grid.
        page_num is a one-based integer
        """
        try:
            page_num = int(page_num)
        except ValueError as err:
            logging.error(err)
            page_num = 1
        if page_num < 1:
            page_num = 1

        try:
            plaques_per_page = int(plaques_per_page)
        except ValueError as err:
            logging.error(err)
            plaques_per_page = DEFAULT_PLAQUES_PER_PAGE
        if plaques_per_page < 1:
            plaques_per_page = 1

        is_admin = users.is_current_user_admin()
        memcache_name = 'view_plaques_page_%s_%s_%s' % (
                            page_num, plaques_per_page, is_admin)
        template_text = memcache.get(memcache_name)
        if template_text is None:
            # Grab all plaques for the map
            plaques = Plaque.approved_list()
            start_index = plaques_per_page * (page_num - 1)
            end_index = start_index + plaques_per_page

            template = JINJA_ENVIRONMENT.get_template('all.html')
            template_values = get_default_template_values(
                                  all_plaques=plaques,
                                  plaques=plaques,
                                  start_index=start_index,
                                  end_index=end_index,
                                  page_num=page_num,
                                  mapzoom=2)

            template_text = template.render(template_values)
            memcache_status = memcache.set(memcache_name, template_text)
            if not memcache_status:
                logging.debug("memcaching for ViewPlaquesPage failed")
        else:
            logging.debug("memcaching worked for ViewPlaquesPage")

        self.response.write(template_text)

class ViewOnePlaqueParent(webapp2.RequestHandler):
    def get(self):
        raise NotImplementedError("Don't call ViewOnePlaqueParent.get directly")

    def _random_plaque_key(self):
        logging.debug("Neither comment_key nor plaque_key is specified. "
                      "Grab a random plaque.")
        count = Plaque.query().count()
        if not count:
            raise ValueError("No plaques!")

        iplaque = random.randint(1, count)
        plaques = Plaque.query().filter(Plaque.approved == True
                               ).order(Plaque.created_on).fetch(offset=iplaque,
                                                                limit=1)
        try:
            plaque_key = plaques[0].key.urlsafe()
        except IndexError as err:
            logging.error(err)
            raise IndexError("No plaques available! Try again later!")

        return plaque_key

    def _get_from_key(self, comment_key=None, plaque_key=None):
        """
        Put the single plaque into a list for rendering so that the common
        map functionality can be used unchanged.
        """
        logging.info("plaque_key=%s" % plaque_key)
        if comment_key is not None:
            comment = ndb.Key(urlsafe=comment_key).get()
            plaque = Plaque.query().filter(Plaque.approved == True
                                  ).filter(Plaque.comments == comment.key
                                  ).get()
        elif plaque_key is not None:
            try:
                old_site_id = int(plaque_key)
                plaque = Plaque.query().filter(Plaque.approved == True
                                      ).filter(Plaque.old_site_id == old_site_id
                                      ).get()
            except ValueError as err:
                plaque = ndb.Key(urlsafe=plaque_key).get()
        else:
            logging.debug("Neither comment_key nor plaque_key is specified. "
                          "Grab a random plaque.")
            key = self._random_plaque_key()
            self.redirect('/plaque/' + key)
            return

        template = JINJA_ENVIRONMENT.get_template('one.html')
        template_values = get_default_template_values(
                              all_plaques=[plaque],
                              plaques=[plaque],
                              mapzoom=8)
        self.response.write(template.render(template_values))

class ViewOnePlaque(ViewOnePlaqueParent):
    """
    Render the single-plaque page from a plaque key, or get a random plaque.
    """
    def get(self, plaque_key=None, ignored_cruft=None):
        logging.info("plaque_key=%s" % plaque_key)
        logging.info("ignored_cruft=%s" % ignored_cruft)
        self._get_from_key(plaque_key=plaque_key)

#class ViewOnePlaqueFromComment(ViewOnePlaqueParent):
#    """
#    Render the single-plaque page from a comment key.
#    """
#    def get(self, comment_key):
#        self._get_from_key(comment_key=comment_key)

class JsonOnePlaque(ViewOnePlaqueParent):
    """
    Render the single-plaque page from a plaque key, or get a random plaque.
    """
    def get(self, plaque_key=None):
        if plaque_key is None:
            plaque_key = self._random_plaque_key()

        plaque = ndb.Key(urlsafe=plaque_key).get()
        self.response.write(
            json.dumps({
                'plaque_key': plaque.key.urlsafe(),
                'title': plaque.title,
            }))

class ViewAllTags(webapp2.RequestHandler):
    def get(self):

        tags_sized = Plaque.all_tags_sized()

        template = JINJA_ENVIRONMENT.get_template('tags.html')
        template_values = get_default_template_values(
                              tags=tags_sized,
                              mapzoom=2)
        self.response.write(template.render(template_values))

class ViewTag(webapp2.RequestHandler):
    def get(self, tag):
        """
        View plaque with a given tag on a grid.
        """
        plaques = Plaque.query().filter(Plaque.approved == True
                               ).filter(Plaque.tags == tag
                               ).order(-Plaque.created_on
                               ).fetch()
        template = JINJA_ENVIRONMENT.get_template('all.html')
        template_values = get_default_template_values(
                              plaques=plaques,
                              start_index=0,
                              end_index=len(plaques),
                              mapzoom=2)
        self.response.write(template.render(template_values))

class About(webapp2.RequestHandler):
    def get(self):
        """
        Render the About page from the common template.
        """
        template = JINJA_ENVIRONMENT.get_template('about.html')
        template_values = get_default_template_values(
                              all_plaques=Plaque.approved_list())
        self.response.write(template.render(template_values))

#class AddComment(webapp2.RequestHandler):
#    @ndb.transactional(xg=True)
#    def post(self):
#        plaque_key = self.request.get('plaque_key')
#        plaque = ndb.Key(urlsafe=plaque_key).get()
#
#        comment_text = self.request.get('comment_text')
#        comment = Comment()
#        comment.text = comment_text
#        comment.put()
#
#        if len(plaque.comments) < 1:
#            plaque.comments = [comment.key]
#        else:
#            plaque.comments.append(comment.key)
#        plaque.put()
#        memcache.flush_all()
#
#        #email_admin(plaque, comment)
#        self.redirect(plaque.page_url)

class AddPlaque(webapp2.RequestHandler):
    """
    Add a plaque entity. Transactional in the _post method.
    """
    def _get_message(self, message):
        if message is None:
            message = self.request.get('message')

        state = self.request.get('state')
        if state is not None:
            if state == ADD_STATE_SUCCESS:
                message = """
                    Hooray! And thank you. We'll geocode your
                    plaque and you'll see it appear on the map shortly
                    <a href="%s">here</a>.
                    """ % message
            elif state == ADD_STATE_ERROR:
                message = """
                      Sorry, your plaque submission had this error: '%s'
                      """ % message
        return message

    def get(self, message=None):
        template = JINJA_ENVIRONMENT.get_template('add.html')
        template_values = get_default_template_values(mapzoom=5)
        message = self._get_message(message)
        if message is not None:
            template_values['message'] = message
        message = self.request.get('message')

        template = JINJA_ENVIRONMENT.get_template('add.html')
        self.response.write(template.render(template_values))

    @ndb.transactional
    def post(self, is_edit=False):
        """
        We set the same parent key on the 'Plaque' to ensure each Plauqe is in
        the same entity group. Queries across the single entity group will be
        consistent. However, the write rate to a single entity group should be
        limited to ~1/second.
        """

        memcache.flush_all()
        try:
            if not is_edit:
                plaqueset_name = self.request.get('plaqueset_name',
                                                  DEFAULT_PLAQUESET_NAME)
                plaque = Plaque(parent=plaqueset_key(plaqueset_name))
            else:
                plaque_key = self.request.get('plaque_key')
                plaque = ndb.Key(urlsafe=plaque_key).get()

            location, created_by, title, description, img_name, img_fh, tags = \
                self._get_form_args()

            plaque.location = location
            plaque.title = title
            plaque.description = description
            plaque.tags = tags
            plaque.approved = users.is_current_user_admin()

            # Upload the image for a new plaque, or update the image for an
            # editted plaque, if specified.
            is_upload_pic = (is_edit and img_name is not None) or (not is_edit)
            if is_upload_pic:
                gcs_fn = plaque.pic if is_edit else None
                gcs_fn, gcs_url = self._upload_image(img_name, img_fh, gcs_fn)
                plaque.pic = gcs_fn
                plaque.img_url = gcs_url

            # Write to the updated_* fields if this is an edit:
            #
            if is_edit:
                plaque.updated_by = users.get_current_user()
                plaque.updated_on = datetime.datetime.now()
                img_rot = self.request.get('img_rot')
                if img_rot is not None and img_rot != 0:
                    plaque.img_rot = int(img_rot)
            else:
                plaque.updated_by = None
                plaque.updated_on = None

            old_site_id = self.request.get('old_site_id', None)
            if old_site_id is not None:
                try:
                    plaque.old_site_id = int(old_site_id)
                except ValueError as err:
                    logging.info('Eating bad ValueError for '
                                 'old_site_id in AddPlaque')
            plaque.put()

            #email_admin(plaque)
            state = ADD_STATES['ADD_STATE_SUCCESS']
            msg = plaque.page_url
        except (BadValueError, ValueError) as err:
            msg = err
            state = ADD_STATES['ADD_STATE_ERROR']
            logging.info(msg)
            # Delete the GCS image, if it exists (the GCS images are not
            # managed by the transaction, apparently)
            try:
                gcs.delete(image.filename)
            except:
                pass

        self.redirect('/add?state=%s&message=%s' % (state, msg))

    def _get_form_args(self):
        """Get the arguments from the form and return them."""
        #latlng = self.request.get('location')
        #lat, lng = [float(l) for l in latlng.split(',')]
        lat = self.request.get('lat')
        lng = self.request.get('lng')
        location = ndb.GeoPt(lat, lng)
        print "lat %s lng %s location %s" % (lat, lng, location)

        if users.get_current_user():
            created_by = users.get_current_user()
        else:
            created_by = None

        title = self.request.get('title')
        if len(title) > 1500:
            title = title[:1499]
        description = self.request.get('description')

        img_file = self.request.POST.get('plaque_image_file')
        img_url = self.request.POST.get('plaque_image_url')

        # Prefer the file to the URL, if both are given.
        #
        if img_file != '':
            img_name = img_file.filename
            img_fh = img_file.file
        elif img_url != '':
            img_name = os.path.basename(img_url)
            img_fh = urllib.urlopen(img_url)
        else:
            img_name = None
            img_fh = None
            #don't do anything (for edits where the image isn't being updated)

        # Get and tokenize tags
        tags_str = self.request.get('tags')
        tags_untokenized = tags_str.split(',')
        tags = [re.sub('\s+', ' ', t.strip().lower()) for t in tags_untokenized]
        tags = [t for t in tags if t] # Remove empties

        return location, created_by, title, description, img_name, img_fh, tags

    def _upload_image(self, img_name, img_fh, gcs_fn=None):
        """
        Upload pic into GCS

        The blobstore.create_gs_key and images.get_serving_url calls are
        outside of the with block; I think this is correct. The
        blobstore.create_gs_key call was erroring out on production when it was
        inside the with block.

        If gcs_fn is specified, overwrite that gcs filename. This is used
        for updating the picture.
        """
        # Kill old image:
        if gcs_fn is not None:
            gcs.delete(gcs_fn)

        date_slash_time = datetime.datetime.now().strftime("%Y%m%d/%H%M%S")
        gcs_fn= '%s/%s/%s' % (GCS_BUCKET, date_slash_time, img_name)

        ct = mimetypes.guess_type(img_name)[0]
        if ct is None:
            ct = 'image/jpeg'
        op = {b'x-goog-acl': b'public-read'}
        with gcs.open(gcs_fn, 'w', content_type=ct, options=op) as gcs_file:
            img_contents = img_fh.read()
            gcs_file.write(img_contents)

        blobstore_gs_key = blobstore.create_gs_key('/gs' + gcs_fn)
        gcs_file_url = images.get_serving_url(blobstore_gs_key)

        return gcs_fn, gcs_file_url

class EditPlaque(AddPlaque):
    """
    Edit a plaque entity. Transactional in the _post method.
    """
    def get(self, plaque_key=None, message=None):
        if plaque_key is None:
            memcache.flush_all()
            self.redirect('/')
            return
        else:
            plaque = ndb.Key(urlsafe=plaque_key).get()
            if plaque is None:
                message = None
            else:
                message = "Editing Plaque"

        template = JINJA_ENVIRONMENT.get_template('add.html')
        template_values = {
            'plaque': plaque,
            'mapzoom': 5,
            'loginout': loginout()
        }
        if message is not None:
            template_values['message'] = message

        template = JINJA_ENVIRONMENT.get_template('edit.html')
        self.response.write(template.render(template_values))

    def post(self):
        memcache.flush_all()
        super(EditPlaque, self).post(is_edit=True)


# TODO: See:
#     http://stackoverflow.com/questions/13305302/using-search-api-python-google-app-engine-big-table
#     https://cloud.google.com/appengine/docs/python/search/ for details
#
#class SearchPlaques(webapp2.RequestHandler):
#    """Run a search in the title and description."""
#    def get(self):
#        raise NotImplementedError("No get method for SearchPlaques")
#
#    def post(self):
#        search_term = self.request.get('search_term')
#        plaques = Plaque.query().filter(Plaque.approved == True
#                               ).filter(Plaque.tags == tag
#                               ).order(-Plaque.created_on
#                               ).fetch()

class FlushMemcache(webapp2.RequestHandler):
    def get(self):
        memcache.flush_all()
        self.redirect('/')

    def post(self):
        memcache.flush_all()
        self.redirect('/')

class Counts(webapp2.RequestHandler):
    def get(self):
        num_comments = Comment.query().count()
        num_plaques = Plaque.query().count()
        num_images = 0
        images = gcs.listbucket(GCS_BUCKET)
        for image in images:
            num_images += 1

        msg = "There are %s comments, %s plaques, %s images" % (
                num_comments, num_plaques, num_images)
        self.response.write(msg)

class DeleteOnePlaque(webapp2.RequestHandler):
    def get(self):
        raise NotImplementedError("no get in DeleteOnePlaque")

    @ndb.transactional
    def post(self):
        """Remove one plaque and its associated Comments and GCS image."""
        plaque_key = self.request.get('plaque_key')
        plaque = ndb.Key(urlsafe=plaque_key).get()
        for comment in plaque.comments:
            comment.delete()
        try:
            gcs.delete(plaque.pic)
        except:
            pass
        plaque.key.delete()
        memcache.flush_all()
        self.redirect('/')

#class DeleteEverything(webapp2.RequestHandler):
#    def get(self):
#        comments = Comment.query().fetch()
#        for comment in comments:
#            comment.key.delete()
#
#        plaques = Plaque.query().fetch()
#        for plaque in plaques:
#            plaque.key.delete()
#
#        num_images = 0
#        images = gcs.listbucket(GCS_BUCKET)
#        for image in images:
#            num_images += 1
#            try:
#                gcs.delete(image.filename)
#            except:
#                pass
#
#        msg = "Deleted %s comments, %s plaques, %s images" % (
#                len(comments), len(plaques), num_images)
#
#        memcache.flush_all()
#        self.response.write(msg)

class ViewPending(webapp2.RequestHandler):
    def get(self):
        plaques = Plaque.pending_list()

        template = JINJA_ENVIRONMENT.get_template('all.html')
        template_values = get_default_template_values(
                              all_plaques=plaques,
                              plaques=plaques,
                              start_index=0,
                              end_index=len(plaques),
                              mapzoom=2)
        template_text = template.render(template_values)
        self.response.write(template_text)

class ApproveAllPending(webapp2.RequestHandler):
    """Approve all pending plaques"""
    def get(self):
        plaques = Plaque.pending_list()
        for plaque in plaques:
            plaque.approved = True
            plaque.put()
        memcache.flush_all()
        self.redirect('/')

class ApprovePending(webapp2.RequestHandler):
    """Approve a plaque"""
    @ndb.transactional
    def post(self):
        plaque_key = self.request.get('plaque_key')
        plaque = ndb.Key(urlsafe=plaque_key).get()
        plaque.approved = True
        plaque.put()
        memcache.flush_all()
        self.redirect('/pending')

class RssFeed(webapp2.RequestHandler):
    def get(self, num_entries=10):
        plaques = Plaque.query(
                      ).filter(Plaque.approved == True
                      ).order(-Plaque.created_on
                      ).fetch(limit=num_entries)
        template = JINJA_ENVIRONMENT.get_template('feed.xml')
        template_values = {'plaques': plaques}
        self.response.write(template.render(template_values))
