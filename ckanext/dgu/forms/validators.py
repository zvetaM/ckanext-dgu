"""
navl validators for the DGU package schema.
"""
import json
from itertools import chain, groupby

from pylons.i18n import _

from ckan.lib.navl.dictization_functions import unflatten, Invalid, \
                                                StopOnError, missing, Missing
from ckan import plugins as p

from ckanext.dgu.lib.helpers import resource_type as categorise_resource
from ckanext.dgu.lib import helpers as dgu_helpers


def to_json(key, data, errors, context):
    try:
        encoded = json.dumps(data[key])
        data[key] = encoded
    except:
        pass

def from_json(key, data, errors, context):
    try:
        encoded = json.loads(data[key])
        data[key] = encoded
    except:
        pass

def value_if_missing(new_value):
    def f(value, context):
        if value is missing or not value:
            return new_value
        return value
    return f


def drop_if_same_as_publisher(key, data, errors, context):
    """
    Validates the contact- and foi- data.

    If it's the same as that on the publisher, then the data is dropped,
    otherwise it's kept, and stored on the dataset (as an extra field).

    For example:

    if key == 'contact-name'.  Then we load the group referenced
    by 'groups__0__name', and then check group.extras['contact-name'].
    """
    from ckan.model.group import Group
    field_name = key[0] # extract from tuple

    group_ref = None
    for ref_name in ['name', 'id']:
        group_ref = data.get(('groups', 0, ref_name), None)
        if group_ref and group_ref is not missing:
            break

    if not group_ref:
        return

    group = Group.get(group_ref)
    if not group:
        return

    if group.extras.get(field_name, None) == data[key]:
        # Remove from data and errors iff the two are equal.
        # If the group doesn't have an extra field for this key,
        # then store it against the dataset.
        data.pop(key, None)
        errors.pop(key, None)
        raise StopOnError

def populate_from_publisher_if_missing(key, data, errors, context):
    """
    If the data is missing, then populate from the publisher.
    """
    from ckan.model.group import Group

    if data[key] is not missing:
        return

    field_name = key[0] # extract from tuple
    group = Group.get(data.get(('groups', 0, 'name'), None))
    if not group:
        return
    data[key] = group.extras.get(field_name, None)


def validate_license(key, data, errors, context):
    """
    Validates and saves properly the selected license options.
    """
    # Unpublished data doesn't need a licence
    if p.toolkit.asbool(data.get(('unpublished',))):
        return

    using_form = ('licence_in_form',) in data
    if using_form:
        licence = data.pop(('licence_in_form',))
        license_id = data.get(('license_id',))
        if license_id and license_id != '__other__':
            # in the form, license_id takes priority over any value in the
            # licence field
            data[('licence',)] = ''
        else:
            # in the form, user has selected 'other'. Transfer the value from
            # 'licence_in_form'.
            data[('licence',)] = licence
    elif ('licence',) in data:
        # licence may be as a key on its own
        licence = data.get(('licence',), '')
    else:
        # licence may be in an extra e.g.
        #   (u'extras', 14, u'key'): u'licence',
        #   (u'extras', 14, u'value'): u'Use limitation; Copyright;',
        licence = _get_extra(data, 'licence', fallback='')

    # 'licence' is free text to go to/from the extra.
    # If there is a 'licence', set licence_id to any detected licence. (for
    # harvested datasets which don't have the licence picker)
    if licence:
        data[('license_id',)], data[('licence',)] = \
            dgu_helpers.get_licence_fields_from_free_text(licence)

    # Require some for of licence, unless this is a UKLP dataset
    if not licence:
        if not data.get(('license_id',)):
            uklp = _get_extra(data, 'UKLP')
            if not uklp:
                errors[('license_id',)] = ['Please provide a licence.']

    # If no license_id, it should be '' because of the 'unicode' validator,
    # otherwise if it is None it saves as u'None'.
    if data.get(('license_id',)) in (None, 'None'):
        data[('license_id',)] = ''

    # Write the licence extra
    licence = data.get(('licence',))
    if licence:
        data[('extras', 99, 'key')] = 'licence'
        data[('extras', 99, 'value')] = licence
    if ('licence',) in data:
        del data[('licence',)]

    return


def _get_extra(data, key, fallback=None):
    i = 0
    while True:
        if ('extras', i, 'key') not in data:
            return None
        if data[('extras', i, 'key')] == key:
            return data.get(('extras', i, 'value'), fallback)
        i += 1


def validate_resources(key, data, errors, context):
    """
    Validates that the timeseries_resources and individual_resources.

    At most one of them should contain resources.
    """
    timeseries_resources = _extract_resources('timeseries', data)
    individual_resources = _extract_resources('individual', data)

    if len(timeseries_resources) and len(individual_resources):
        errors[('validate_resources',)] = ['Only define timeseries or individual '
                                               'resources, not both']

def merge_resources(key, data, errors, context):
    """
    Merges additional resources and data resources into a single entry in the data dict.

    And removes the '{additional,timeseries,individual}_resources' entries.

    This post-processing only occurs if there have been no validation errors.
    This prevents us losing the user's input.
    """
    if key != ('__after',):
        raise Exception('The merge_resources function should only be '
                        'called as a post-processing function.  '
                        'Called with "%s"' % key)

    for value in errors.values():
        if value:
            return

    _merge_dict(data)
    _merge_dict(errors)

def _merge_dict(d):
    """
    Helper function that performs a resource merge on the given dict.

    A resource merge takes a flattened dictionary, with keys (tuples) of the
    form `('additional_resource', 0, 'url')` and `('timeseries_resource', 0, 'url')`.
    And transforms it into a dict with the above keys merged into ones of the
    form `('resources', 0, 'url')`.

    d is the dict to perform the merge on.
    """
    additional_resources = _extract_resources('additional', d)
    timeseries_resources = _extract_resources('timeseries', d)
    individual_resources = _extract_resources('individual', d)
    resources = sorted(chain(additional_resources,
                             timeseries_resources,
                             individual_resources))

    # group by the first two items in the flattened key
    #  - num : from enumerate.
    #  - resource : key we've grouped on, e.g. ('additional_resource', 0)
    #  - values : iterator over the resource keys,, e.g.
    #             [ ('additional_resource', 0, 'url'),
    #               ('additional_resource', 0, 'description') ]
    for (num, (resource, values)) in enumerate(groupby(resources, lambda t: t[:2])):
        resource_type, original_index = resource
        for (_,_,field) in values:
            d[('resources', num, field)] = d[(resource_type, original_index, field)]

            # delete the original key from the d, e.g.
            del d[(resource_type, original_index, field)]

def unmerge_resources(key, data, errors, context):
    """
    Splits the merged resources back into their respective resource types.

    It leaves the 'resources' entry there too, for compatibility with other
    sites harvesting DGU etc.

    This post-processing only occurs if there have been no validation errors.
    """
    if key != ('__after',):
        raise Exception('The unmerge_resources function should only be '
                        'called as a post-processing function.  '
                        'Called with "%s"' % key)

    for value in errors.values():
        if value:
            return

    # data[('resources', '0', 'url')]

    # Categorise each resource, and add it to the respective entry
    unflattened_resources = unflatten(data).get('resources', [])
    error_resources = unflatten(errors).get('resources', [])
    resources = zip(unflattened_resources,
                    map(categorise_resource, unflattened_resources),
                    error_resources)

    for resource_type in ('additional', 'timeseries', 'individual'):
        match = lambda (r,t,e): t == resource_type # match resources of this resource_type
        for index, (resource,_,error_resource) in enumerate(filter(match, resources)):
            for field in resource.keys():
                data_key = ('%s_resources'%resource_type, index, field)
                data[data_key] = resource[field]
            for field in error_resource.keys():
                error_key = ('%s_resources'%resource_type, index, field)
                errors[error_key] = []

    for key in ( key for key in errors.keys() if key[0] == 'resources' ):
        del errors[key]

def _validate_resource_types(allowed_types, default=None):
    """
    Returns a function that validates the given resource_type is allowed.

    If a resource_type is False-like, then it returns a default value when
    available.
    """

    def _converter(value):
        if not value and default:
            return default
        elif value not in allowed_types:
            raise Invalid(_('Invalid resource type: %s' % value))
        return value
    return _converter

validate_additional_resource_types = _validate_resource_types(
                                         ('documentation',),
                                         default='documentation')

validate_data_resource_types = _validate_resource_types(
                                   ('api','file'),
                                   default='file')

def _extract_resources(name, data):
    """
    Extracts the flattened resources with the given name from the flattened data dict
    """
    return [ key for key in data.keys() if key[0] == name+'_resources' ]

def remove_blank_resources(key, data, errors, context):
    '''
    If the user leaves values for a resource blank, then remove it plus any
    validation errors.
    '''
    assert key == ('__after',)
    # needs to be run after resource validation so it can delete validation
    # errors blank resources

    additional_resources = _extract_resources('additional', data)
    timeseries_resources = _extract_resources('timeseries', data)
    individual_resources = _extract_resources('individual', data)
    resources = sorted(chain(additional_resources,
                             timeseries_resources,
                             individual_resources))

    user_filled_fields = set(('description', 'format', 'url', 'date'))

    for (resource, values_iter) in groupby(resources, lambda t: t[:2]):
        resource_type, original_index = resource
        is_blank_resource = True
        values = list(values_iter) # copy it - we need it twice
        for (_,_,field) in values:
            if field not in user_filled_fields:
                continue
            field_value = data[(resource_type, original_index, field)]
            if field_value.strip() if isinstance(field_value, basestring) else field_value:
                is_blank_resource = False
                break
        if is_blank_resource:
            for (_,_,field) in values:
                triple = (resource_type, original_index, field)
                del data[triple]
                if triple in errors:
                    del errors[triple]

categories = (
              #('core-department', 'UK Government Core Department'),
              #('non-core-department', 'UK Government Non-Core Department'),
              ('ministerial-department', 'Ministerial department'),
              ('non-ministerial-department', 'Non-ministerial department'),
              #('devolved', 'Devolved Government Body'),
              ('devolved', 'Devolved administration'),
              #('alb', 'Arm\'s Length Body (includes Executive Agencies, Non-Departmental Public Bodies, Trading Funds and NHS bodies)'),
              ('executive-ndpb', 'Executive non-departmental public body'),
              ('advisory-ndpb', 'Advisory non-departmental public body'),
              ('tribunal-ndpb', 'Tribunal non-departmental public body'),
              ('executive-agency', 'Executive agency'),
              ('executive-office', 'Executive office'),
              # gov.uk has no Local Council, so added it
              ('local-council', 'Local authority'),
              # gov.uk has no NHS, and CCGs are Statutory Bodies, but there are
              # still trusts which aren't
              ('nhs', 'NHS body'),
              ('gov-corporation', 'Public corporation'),
              # gov.uk has no NGOs, so add this here. e.g. Canal and River Trusts
              ('charity-ngo', 'Charity or Non-Governmental Organisation'),
              ('private', 'Private Sector'),
              ('grouping', 'A notional grouping of organisations'),
              ('sub-organisation', 'Sub-organisation'),
              # other: enquiries, public-private-partnerships
              ('other', 'Other'),
              )

def validate_publisher_category(key, data, errors, context):
    '''
    Validates the category field is a valid value.
    '''
    category = data[('category',)]
    if category not in dict(categories).keys():
        if category:
            errors[('category',)] = ['Category is not valid.']

def dgu_boolean_validator(value, context):
    """
    This validators the data coming in and out of the form in such
    a way that if it is not positive (true, yes, etc) then False
    is returned.  This enables checkboxes to be used for booleans
    whereas before the browser didn't send a value (when field
    unchecked) which resulted in a validation error.

    True, true, 1, y -> True
    False, false, 0, n -> True
    Not specified -> False
    anything else -> False
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, Missing):
        return False
    if value.lower() in ['true', 'yes', 't', 'y', '1']:
        return True
    return False

def bool_(key, data, errors, context):
    '''
    True, true, 1, y -> True
    False, false, 0, n -> True
    Not specified -> False
    anything else -> validation error raised
    '''
    value = data[key]
    if value in ('', None):
        data[key] = 'false'
        return
    try:
        true_or_false = p.toolkit.asbool(value)
        data[key] = str(true_or_false).lower()
    except ValueError:
        errors[key] = ['Must be true or false']
