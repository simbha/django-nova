"""
Basic model for a newsletter subscription, minus the newsletter (for now).
More to come...

project specific settings:
NOVA_CONTEXT_PROCESSORS:
    defines a list of functions (similar to django's TEMPLATE_CONTEXT_PROCESSORS) that are called
    when NewsletterIssues render themselves, just before they are sent.  Each function must accept
    the following arguments:
        newsletter_issue: NewsletterIssue instance that is sending the email
        email: EmailAddress instance that is receiving the email
"""
from datetime import datetime
from subprocess import Popen, PIPE

from django.db import models
from django.forms import ValidationError
from django.conf import settings
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.core.mail import send_mail, EmailMessage
from django.core.validators import email_re
from django.utils.translation import ugettext_lazy as _
from django.template import Context, Template, TemplateDoesNotExist
from django.template.loader import find_template_loader

from nova.helpers import *

TOKEN_LENGTH = 12

def _sanitize_email(email):
    return email.strip().lower()

def _email_is_valid(email):
    return email_re.match(email)


class EmailAddressManager(models.Manager):
    def create_with_random_token(self, email, **kwargs):
        """
        Generates a new (unconfirmed) email address with a random, unique token.
        Uses the underlying database uniqueness constraints to prevent race
        conditions on token creation.
        """
        instance = None

        while instance is None:
            try:
                # the `make_random_password` method returns random strings that
                # are at least somewhat readable and re-typeable
                token = User.objects.make_random_password(length=TOKEN_LENGTH)
                instance = self.create(email=email, token=token, **kwargs)
            except ValidationError:
                continue

        return instance


class EmailAddress(models.Model):
    """
    A basic model for confirmed email addresses.
    """
    email = models.EmailField(unique=True)
    user = models.ForeignKey(User, null=True, blank=True)
    token = models.CharField(null=True, blank=True, unique=True, max_length=TOKEN_LENGTH)
    client_addr = models.CharField(max_length=16, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    confirmed = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    reminders_sent = models.PositiveIntegerField(default=0)

    #auto_now_add so we don't remind some user immediately after they sign up
    reminded_at = models.DateTimeField(auto_now_add=True)

    objects = EmailAddressManager()

    def save(self, *args, **kwargs):
        """
        Set confirmed_at when this instance is confirmed.
        """
        self.email = _sanitize_email(self.email)

        if self.confirmed:
            if self.confirmed_at is None:
                self.confirmed_at = datetime.now()        
            try:
                #set up a user account if it exists
                self.user = User.objects.get(email=self.email)
            except User.DoesNotExist:
                #create one if it doesn't
                #TODO: provide notification to these users, letting them know
                #that they need to reset their password
                username = self._create_unique_username_from_email(self.email)
                self.user = User.objects.create_user(username, self.email)
                self.user.set_unusable_password()
                self.user.save()
            
            except User.MultipleObjectsReturned:
                #multiple users with the same email!
                self.user = User.objects.filter(email=self.email)[0]
                
        super(EmailAddress, self).save(*args, **kwargs)

    @property
    def confirm_url(self):
        """
        Returns the unique confirmation URL for this email address,
        suitable for use in follow-up emails.
        """
        return reverse('nova.views.confirm', args=(self.token,))

    @property
    def unsubscribe_url(self):
        """
        Returns the unique unsubscribe URL for this email address,
        suitable for use in follow-up emails.
        """
        return reverse('nova.views.unsubscribe', args=(self.token,))

    def subscribe(self, newsletter):
        """
        Subscribe this email address to a newsletter.
        :return: (Subscription, created)
        """
        return Subscription.objects.get_or_create(email_address=self,
                newsletter=newsletter)

    def unsubscribe(self, newsletter=None):
        """
        Unsubscribe this email address from a specific newsletter.
        If newsletter is None, unsubscribe from all.
        """
        if not newsletter:
            self.subscriptions.all().delete()
        else:
            self.subscriptions.filter(newsletter=newsletter).delete()
            
    def _create_unique_username_from_email(self, email):
        """
        Ensure there are no collisions on user name and
        make certain the username is less than thirty characters long
        """
        unique = False
        #truncate email to thirty characters
        parts = email.split('@')
        username = parts[0][:30]
        
        i = 0
        while not unique:
            #generate a number to append to the end of the username
            i_string = "%s" % i
            try:
                #ensure uniqueness
                user = User.objects.get(username=username)
                
                end = 29 - len(i_string)
                username = "%s%s" % (username[:end], i)
                i += 1
            except User.DoesNotExist:
                unique = True
        return username
    
    def __unicode__(self):
        """
        String-ify this email address
        """

        if self.confirmed:
            status = u'confirmed'
        else:
            status = u'unconfirmed'

        return u'{0} ({1})'.format(self.email or 'None', status)

    class Meta:
        verbose_name_plural = 'Email Addresses'


class Newsletter(models.Model):
    """
    A basic newsletter model.
    :todo: Change default_template to a TextField?
    """
    title = models.CharField(max_length=255, blank=False)
    active = models.BooleanField(blank=False)
    from_email = models.CharField(max_length=255, blank=False, default=settings.NOVA_FROM_EMAIL,
            help_text=_("The address that issues of this newsletter will be sent from."))
    reply_to_email = models.CharField(max_length=255, blank=True,
            help_text=_("The reply to address that will be set for all issues of this newsletter."))
    approvers = models.TextField(blank=True,
        help_text=_("A whitespace separated list of email addresses."))
    default_template = models.CharField(max_length=255, blank=True,
        help_text=_("The name of a default template to use for issues of this newsletter."))
    created_at = models.DateTimeField(auto_now_add=True)

    subscriptions = models.ManyToManyField(EmailAddress, through='Subscription')

    def save(self, *args, **kwargs):
        """
        If the reply_to_email is blank, set to from_email
        """
        if not self.reply_to_email:
            self.reply_to_email = self.from_email

        super(Newsletter, self).save(*args, **kwargs)

    @property
    def subscribers(self):
        """
        Return a list of confirmed subscribers.
        """
        return self.subscriptions.filter(confirmed=True)

    def __unicode__(self):
        """
        String-ify this newsletter
        """
        return u'%s' % self.title

class NewsletterIssue(models.Model):
    """
    An issue of a newsletter. This model wraps the actual email subject
    line and template that will be sent out to subscribers.
    """
    newsletter = models.ForeignKey(Newsletter)
    subject = models.CharField(max_length=255, null=False, blank=False)
    template = models.TextField(null=False, blank=True,
        help_text=_("If template is left empty we'll use the default template from the parent newsletter."))
    rendered_template = models.TextField(null=True, blank=True)
    
    track = models.BooleanField(default=True,
        help_text=_("Add link tracking to all links from this domain."))
    tracking_domain = models.CharField(max_length=255, blank=True, 
        help_text=_("The domain for which links should be tracked."))
    tracking_campaign = models.CharField(max_length=20, blank=True, 
        help_text=_("A short keyword to identify this campaign (e.g. 'DHD')."))

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(blank=True, null=True,
            help_text=_("When this newsletter issue was last sent to subscribers."))

    def save(self, *args, **kwargs):
        """
        If template is blank and the parent newsletter has a default
        template, load that default from disk.
        """
        if not self.template:
            if self.newsletter.default_template:
                try:
                    self.template = get_raw_template(self.newsletter.default_template)
                except:
                    pass

        super(NewsletterIssue, self).save(*args, **kwargs)

        if self.template:
            self.rendered_template = self.render(track=self.track)
            super(NewsletterIssue, self).save()

    def premail(self, body_text=None, plaintext=False):
        """
        Run 'premailer' on the specified email body to format html to be readable by email clients
        """
        if not body_text:
            body_text = self.template

        args = ['premailer', '--mode', 'txt' if plaintext else 'html']

        p = Popen(args, stdin=PIPE, stdout=PIPE, stderr=PIPE)
        premailed, err = p.communicate(input=str(body_text))

        if p.returncode != 0:
            raise PremailerException(err)
        else:
            return premailed

    def render(self, template=None, canonicalize=True, track=True, premail=True, extra_context=None):
        """
        Render a django template into a formatted newsletter issue.
        Uses the setting NOVA_CONTEXT_PROCESSORS to load a list of functions, similar to django's
        template context processors to add extra values to the context dictionary.
        """
        if not template:
            template = self.template

        context = Context({
            'issue': self,
        })

        if extra_context:
            context.update(extra_context)

        # Load extra context processors
        for context_processor in getattr(settings, 'NOVA_CONTEXT_PROCESSORS', []):
            module, attr = context_processor.rsplit('.', 1)
            module = __import__(module, fromlist=[attr])
            processor = getattr(module, attr)
            context.update(processor(newsletter_issue=self))

        template = Template(template)
        rendered_template = template.render(context)

        # Canonicalize relative links
        if canonicalize:
            rendered_template = canonicalize_links(rendered_template)

        # Track links
        if track:
            rendered_template = track_document(rendered_template, domain=self.tracking_domain,
                    campaign=self.tracking_campaign, source='newsletter-%s' % (self.newsletter.pk,))

        # Run premailer
        if premail:
            rendered_template = self.premail(body_text=rendered_template)

        return rendered_template

    def send(self, email_addresses=None, extra_headers=None, mark_as_sent=True):
        """
        Sends this issue to subscribers of this newsletter. 

        :param email_addresses: A list of EmailAddress objects to be used as the recipient list.
        :param extra_headers: Any extra mail headers to be used.
        :param mark_as_sent: Whether to record this issue as sent.

        :todo: This method is not very performant as it has to render a template
        for every recipient of a newsletter. Investigate any options to make this
        method more performant.
        """
        headers = {
            'Reply-To': self.newsletter.reply_to_email,        
        }

        # Set any extra headers
        if extra_headers:
            headers.update(extra_headers)

        # Default to sending to all active subscribers
        if not email_addresses:
            email_addresses = self.newsletter.subscribers

        for send_to in email_addresses:
                # Render the newsletter for this subscriber
                rendered_template = self.render(track=self.track,
                        premail=False, extra_context={'email': send_to})

                # Get html and plaintext templates
                html_template = self.premail(rendered_template, plaintext=False)
                plaintext_template = self.premail(rendered_template, plaintext=True)

                # Send multipart message
                send_multipart_mail(self.subject,
                        txt_body=plaintext_template,
                        html_body=html_template,
                        from_email=self.newsletter.from_email,
                        headers=headers,
                        recipient_list=(send_to.email,))

        # Update sent_at timestamp
        if mark_as_sent:
            self.sent_at = datetime.now()
            self.save()

    def send_test(self):
        """
        Sends this issue to an email address specified by an admin user
        """
        email_addresses = []
        approvers = self.newsletter.approvers.split()
        for approver_email in approvers:
            try:
                send_to = EmailAddress.objects.get(email=approver_email.strip(','))
            except EmailAddress.DoesNotExist:
                send_to = EmailAddress.objects.create_with_random_token(email=approver_email.strip(','))

            email_addresses.append(send_to)

        self.send(email_addresses, mark_as_sent=False)

    def __unicode__(self):
        """
        String-ify this newsletter issue
        """
        return u'%s' % self.subject

    def get_absolute_url(self):
        return reverse('nova.views.preview', args=[self.id])


class Subscription(models.Model):
    """
    This model subscribes an EmailAddress instance to a Newsletter instance.
    """
    email_address = models.ForeignKey(EmailAddress, related_name='subscriptions')
    newsletter = models.ForeignKey(Newsletter)
    active = models.BooleanField(null=False, blank=False, default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    def __unicode__(self):
        """
        String-ify this subscription
        """
        return u'{email} to {newsletter})'.format(email=self.email_address,
                                                  newsletter=self.newsletter)

def get_raw_template(name, dirs=None):
    """
    Uses Django's template loaders to find and return the
    raw template source. 
    """
    for loader_name in settings.TEMPLATE_LOADERS:
        loader = find_template_loader(loader_name)
        if loader is not None:
            try:
                return loader.load_template_source(name)[0]
            except TemplateDoesNotExist:
                pass
    raise TemplateDoesNotExist(name)
