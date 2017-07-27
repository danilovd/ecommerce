import itertools
import mock

from analytics import Client
from oscar.core.loading import get_class, get_model
from oscar.test import factories

from ecommerce.core.tests import toggle_switch
from ecommerce.courses.tests.factories import CourseFactory
from ecommerce.extensions.analytics.utils import parse_tracking_context, translate_basket_line_for_segment
from ecommerce.extensions.api.v2.tests.views.mixins import CatalogMixin
from ecommerce.extensions.basket.models import Basket
from ecommerce.extensions.basket.tests.mixins import BasketMixin
from ecommerce.extensions.test.factories import create_basket
from ecommerce.tests.factories import SiteConfigurationFactory
from ecommerce.tests.testcases import TestCase

Basket = get_model('basket', 'Basket')
OrderNumberGenerator = get_class('order.utils', 'OrderNumberGenerator')


class BasketTests(CatalogMixin, BasketMixin, TestCase):
    def setUp(self):
        super(BasketTests, self).setUp()
        toggle_switch('fire_non_order_events', True)
        self.site1 = SiteConfigurationFactory().site
        self.site2 = SiteConfigurationFactory().site

    def assert_basket_state(self, basket, status, user, site):
        """ Verify the given basket's properties. """
        self.assertEqual(basket.status, status)
        self.assertEqual(basket.owner, user)
        self.assertEqual(basket.site, site)

    def test_order_number(self):
        """ The method should return the order number for the Order corresponding to the Basket. """
        basket = self.create_basket(owner=self.create_user(), site=self.site1)
        expected = OrderNumberGenerator().order_number(basket)
        self.assertEqual(basket.order_number, expected)

    def test_unicode(self):
        """ Verify the __unicode__ method returns the correct value. """
        basket = self.create_basket(owner=self.create_user(), site=self.site1)
        expected = u"{id} - {status} basket (owner: {owner}, lines: {num_lines})".format(
            id=basket.id,
            status=basket.status,
            owner=basket.owner,
            num_lines=basket.num_lines
        )

        self.assertEqual(unicode(basket), expected)

    def test_get_basket_without_existing_baskets(self):
        """ If the user has no existing baskets, the method should return a new one. """
        user = factories.UserFactory()
        self.assertEqual(user.baskets.count(), 0, 'A new user should not have any associated Baskets.')

        basket = Basket.get_basket(user, self.site1)

        # Check the basic details of the new basket
        self.assert_basket_state(basket, Basket.OPEN, user, self.site1)

        self.assertEqual(len(basket.all_lines()), 0, 'The new basket should be empty')
        self.assertEqual(user.baskets.count(), 1, 'No basket was created for the user.')

        # Verify we create new baskets for other sites/tenants
        basket = Basket.get_basket(user, self.site2)
        self.assert_basket_state(basket, Basket.OPEN, user, self.site2)
        self.assertEqual(len(basket.all_lines()), 0, 'The new basket should be empty')
        self.assertEqual(user.baskets.count(), 2, 'A new basket was not created for the second site.')

    def test_get_basket_with_existing_baskets(self):
        """ If the user has existing baskets in editable states, the method should return a single merged basket. """
        user = factories.UserFactory()

        # Create baskets in a state that qualifies them for merging
        editable_baskets = []
        for status in Basket.editable_statuses:
            editable_baskets.append(self.create_basket(user, self.site1, status))

        # Create baskets that should NOT be merged
        non_editable_baskets = []
        for status in (Basket.MERGED, Basket.FROZEN, Basket.SUBMITTED):
            basket = self.create_basket(user, self.site1)
            basket.status = status
            basket.save()
            non_editable_baskets.append(basket)

        # Create a basket for the other site/tenant
        Basket.get_basket(user, self.site2)

        self.assertEqual(user.baskets.count(), 6)

        basket = Basket.get_basket(user, self.site1)

        # No new basket should be created
        self.assertEqual(user.baskets.count(), 6)

        # Check the basic details of the new basket
        self.assertEqual(basket.status, Basket.OPEN)
        self.assertEqual(basket.owner, user)

        # One of the previously editable baskets should be in the merged state.
        actual_states = [Basket.objects.get(id=eb.id).status for eb in editable_baskets]
        self.assertEqual(actual_states, [Basket.OPEN, Basket.MERGED])

        # The merged basket should include the products from the original baskets
        expected_lines = list(itertools.chain.from_iterable([list(eb.lines.all()) for eb in editable_baskets]))
        self.assertEqual(list(basket.lines.all()), expected_lines)

        # Verify the basket for the second site/tenant is not modified
        self.assert_basket_state(user.baskets.get(site=self.site2), Basket.OPEN, user, self.site2)

    def test_create_basket(self):
        """ Verify the method creates a new basket. """
        user = factories.UserFactory()
        basket = Basket.create_basket(self.site1, user)
        self.assertEqual(basket.site, self.site1)
        self.assertEqual(basket.owner, user)

    def test_flush_with_product(self):
        """
        Verify the method fires 'Product Removed' Segment event with the correct information when basket is not empty
        """
        self.site1.siteconfiguration = SiteConfigurationFactory()
        self.site1.siteconfiguration.segment_key = 'fake_key'
        basket = create_basket(empty=True)
        basket.owner = factories.UserFactory()
        basket.site = self.site1
        basket.save()

        course = CourseFactory()
        seat = course.create_or_update_seat('verified', True, 100, self.partner)
        basket.add_product(seat)

        properties = translate_basket_line_for_segment(basket.lines.first())
        user_tracking_id, lms_client_id, lms_ip = parse_tracking_context(basket.owner)
        context = {
            'ip': lms_ip,
            'Google Analytics': {
                'clientId': lms_client_id
            }
        }

        with mock.patch.object(Client, 'track') as mock_track:
            basket.flush()
            mock_track.assert_called_once_with(user_tracking_id, 'Product Removed', properties, context=context)

    def test_flush_without_product(self):
        """ Verify the method does not fireSegment event when basket is empty """
        self.site1.siteconfiguration = SiteConfigurationFactory()
        self.site1.siteconfiguration.segment_key = 'fake_key'

        basket = create_basket(empty=True)
        basket.owner = factories.UserFactory()
        basket.site = self.site1

        with mock.patch.object(Client, 'track') as mock_track:
            basket.flush()
            self.assertEqual(mock_track.call_count, 0)

    def test_add_product(self):
        """ Verify the method fires Product Added analytic event when a product is added to the basket """
        course = CourseFactory()
        basket = create_basket(empty=True)
        seat = course.create_or_update_seat('verified', True, 100, self.partner)
        with mock.patch('ecommerce.extensions.basket.models.track_segment_event') as mock_track:
            basket.add_product(seat)
            properties = translate_basket_line_for_segment(basket.lines.first())
            properties['cart_id'] = basket.id
            mock_track.assert_called_once_with(basket.site, basket.owner, 'Product Added', properties)
