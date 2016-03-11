# -*- coding: utf-8 -*-
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division

import json

from django.core.exceptions import ValidationError, MultipleObjectsReturned
from django.contrib.gis.db import models
from django.contrib.gis.geos import Point, Polygon
from django.utils.translation import ugettext as _
from django.db import transaction

from treemap.models import Species, Plot, Tree, MapFeature
from treemap.lib.object_caches import udf_defs

from importer.models.base import GenericImportRow, GenericImportEvent
from importer import fields
from importer import errors


class TreeImportEvent(GenericImportEvent):
    """
    A TreeImportEvent represents an attempt to upload a csv containing
    tree/plot information
    """

    import_schema_version = 1  # Update if any column header name changes
    import_type = 'tree'

    plot_length_conversion_factor = models.FloatField(default=1.0)
    plot_width_conversion_factor = models.FloatField(default=1.0)
    diameter_conversion_factor = models.FloatField(default=1.0)
    tree_height_conversion_factor = models.FloatField(default=1.0)
    canopy_height_conversion_factor = models.FloatField(default=1.0)

    class Meta:
        app_label = 'importer'

    def row_set(self):
        return self.treeimportrow_set

    def __unicode__(self):
        return _('Tree Import #%s') % self.pk

    def get_udf_column_name(self, udf_def):
        # Prefix with model name, e.g. "Density" -> "Tree: Density"
        model_name = udf_def.model_type.lower()
        if model_name == 'plot':
            model_name = 'planting site'
        return "%s: %s" % (model_name, udf_def.name.lower())

    def ordered_legal_fields(self):
        def udf_column_names(model_name):
            return tuple(self.get_udf_column_name(udf_def)
                         for udf_def in udf_defs(self.instance, model_name)
                         if not udf_def.iscollection)

        plot_udfs = udf_column_names('Plot')
        tree_udfs = udf_column_names('Tree')

        return fields.trees.ALL + plot_udfs + tree_udfs

    def legal_and_required_fields(self):
        legal_fields = set(self.ordered_legal_fields())

        return (legal_fields, {fields.trees.POINT_X, fields.trees.POINT_Y})


class TreeImportRow(GenericImportRow):
    WARNING = 2

    PLOT_MAP = {
        'geom': fields.trees.POINT,
        'width': fields.trees.PLOT_WIDTH,
        'length': fields.trees.PLOT_LENGTH,
        # TODO: READONLY restore when implemented
        # 'readonly': fields.trees.READ_ONLY,
        'owner_orig_id': fields.trees.EXTERNAL_ID_NUMBER,
        'address_street': fields.trees.STREET_ADDRESS,
        'address_city': fields.trees.CITY_ADDRESS,
        'address_zip': fields.trees.POSTAL_CODE
    }

    TREE_MAP = {
        'diameter': fields.trees.DIAMETER,
        'height': fields.trees.TREE_HEIGHT,
        'canopy_height': fields.trees.CANOPY_HEIGHT,
        'species': fields.trees.SPECIES_OBJECT,
        'date_planted': fields.trees.DATE_PLANTED,
        'date_removed': fields.trees.DATE_REMOVED,
        # TODO: READONLY restore when implemented
        # 'readonly': fields.trees.READ_ONLY
    }

    # plot that was created from this row
    plot = models.ForeignKey(Plot, null=True, blank=True)

    # The main import event
    import_event = models.ForeignKey(TreeImportEvent)

    class Meta:
        app_label = 'importer'

    @property
    def model_fields(self):
        return fields.trees

    def commit_row(self):
        is_valid = self.validate_row()

        if not is_valid:
            return  # not ready to commit

        if self.status == TreeImportRow.SUCCESS:
            return  # nothing changed so no need to commit

        # Get our data
        data = self.cleaned

        self.convert_units(data, {
            fields.trees.PLOT_WIDTH:
            self.import_event.plot_width_conversion_factor,

            fields.trees.PLOT_LENGTH:
            self.import_event.plot_length_conversion_factor,

            fields.trees.DIAMETER:
            self.import_event.diameter_conversion_factor,

            fields.trees.TREE_HEIGHT:
            self.import_event.tree_height_conversion_factor,

            fields.trees.CANOPY_HEIGHT:
            self.import_event.canopy_height_conversion_factor
        })

        plot_id = data.get(self.model_fields.OPENTREEMAP_PLOT_ID, None)

        # Check for an existing plot, use it if we're not already:
        if plot_id and (self.plot is None or self.plot.pk != plot_id):
            plot = Plot.objects.get(pk=plot_id)
        elif self.plot is not None:
            plot = self.plot
        else:
            plot = Plot(instance=self.import_event.instance)

        self._commit_row(data, plot)

    @transaction.atomic
    def _commit_row(self, data, plot):
        self._commit_plot_data(data, plot)
        # TREE_PRESENT handling:
        #   If True, create a tree
        #   If False, don't create a tree
        #   If empty or missing, create a tree if a tree field is specified
        tree = plot.current_tree()
        tree_edited = False
        tree_present = data.get(self.model_fields.TREE_PRESENT, None)
        if tree_present:
            tree_edited = True
            if tree is None:
                tree = Tree(instance=plot.instance)

        if tree_present or tree_present is None:
            self._commit_tree_data(data, plot, tree, tree_edited)

        self.plot = plot
        self.status = TreeImportRow.SUCCESS
        self.save()

    def _import_value_to_udf_value(self, udf_def, value):
        if udf_def.datatype_dict['type'] == 'multichoice':
            # multichoice fields are represented in the import file as
            # a string, but the `udfs` attribute on the model expects
            # an actual list.
            if value:
                return json.loads(value)
            else:
                return None
        else:
            return value

    def _commit_plot_data(self, data, plot):
        plot_edited = False
        for plot_attr, field_name in TreeImportRow.PLOT_MAP.iteritems():
            value = data.get(field_name, None)
            if value:
                plot_edited = True
                setattr(plot, plot_attr, value)

        ie = self.import_event
        plot_udf_defs = udf_defs(ie.instance, 'Plot')
        for udf_def in plot_udf_defs:
            udf_column_name = ie.get_udf_column_name(udf_def)
            value = data.get(udf_column_name, None)
            if value:
                plot_edited = True
                plot.udfs[udf_def.name] = self._import_value_to_udf_value(
                    udf_def, value)

        if plot_edited:
            plot.save_with_system_user_bypass_auth()

    def _commit_tree_data(self, data, plot, tree, tree_edited):
        for tree_attr, field_name in TreeImportRow.TREE_MAP.iteritems():
            value = data.get(field_name, None)
            if value:
                tree_edited = True
                if tree is None:
                    tree = Tree(instance=plot.instance)
                setattr(tree, tree_attr, value)

        ie = self.import_event
        tree_udf_defs = udf_defs(ie.instance, 'Tree')
        for udf_def in tree_udf_defs:
            udf_column_name = ie.get_udf_column_name(udf_def)
            value = data.get(udf_column_name, None)
            if value:
                tree_edited = True
                if tree is None:
                    tree = Tree(instance=plot.instance)
                tree.udfs[udf_def.name] = \
                    self._import_value_to_udf_value(udf_def, value)

        if tree_edited:
            tree.plot = plot
            tree.save_with_system_user_bypass_auth()

    def validate_geom(self):
        x = self.cleaned.get(fields.trees.POINT_X, None)
        y = self.cleaned.get(fields.trees.POINT_Y, None)

        # Note, this shouldn't really happen since main
        # file validation will fail, but butter safe than sorry
        if x is None or y is None:
            self.append_error(errors.MISSING_FIELD,
                              (fields.trees.POINT_X, fields.trees.POINT_Y))
            return False

        # Simple validation
        # longitude must be between -180 and 180
        # latitude must be betwen -90 and 90
        if abs(x) > 180 or abs(y) >= 90:
            self.append_error(errors.INVALID_GEOM,
                              (fields.trees.POINT_X, fields.trees.POINT_Y))
            return False

        p = Point(x, y, srid=4326)
        p.transform(3857)

        if self.import_event.instance.bounds.geom.contains(p):
            self.cleaned[fields.trees.POINT] = p
        else:
            self.append_error(errors.GEOM_OUT_OF_BOUNDS,
                              (fields.trees.POINT_X, fields.trees.POINT_Y))
            return False

        return True

    def validate_otm_id(self):
        oid = self.cleaned.get(fields.trees.OPENTREEMAP_PLOT_ID, None)

        if oid:
            has_plot = Plot.objects \
                .filter(pk=oid, instance=self.import_event.instance) \
                .exists()

            if not has_plot:
                self.append_error(errors.INVALID_OTM_ID,
                                  fields.trees.OPENTREEMAP_PLOT_ID)
                return False

        return True

    def validate_proximity(self, point):
        # This block must stay at the top of the function and
        # effectively disables proximity validation when the import
        # row includes an OTM plot id. Proximity validation can
        # prevent instance admins from correcting the locations of
        # previously uploaded trees in bulk.
        oid = self.cleaned.get(fields.trees.OPENTREEMAP_PLOT_ID, None)
        if oid is not None:
            return True

        plot_ids_from_this_import = TreeImportRow.objects\
            .filter(import_event=self.import_event)\
            .filter(plot__isnull=False)\
            .values_list('plot__pk', flat=True)

        offset = 3.048  # 10ft in meters
        nearby_bbox = Polygon(((point.x - offset, point.y - offset),
                               (point.x - offset, point.y + offset),
                               (point.x + offset, point.y + offset),
                               (point.x + offset, point.y - offset),
                               (point.x - offset, point.y - offset)))

        # Using MapFeature directly avoids a join between the
        # treemap_plot and treemap_mapfeature tables.
        nearby = MapFeature.objects\
                           .filter(instance=self.import_event.instance)\
                           .filter(feature_type='Plot')\
                           .filter(geom__intersects=nearby_bbox)\
                           .exclude(pk__in=plot_ids_from_this_import)\

        nearby = nearby.distance(point).order_by('distance')[:5]

        if len(nearby) > 0:
            flds = (fields.trees.POINT_X, fields.trees.POINT_Y)
            if nearby[0].distance.m < 0.001:
                self.append_error(errors.DUPLICATE_TREE, flds)
            else:
                self.append_error(errors.NEARBY_TREES, flds,
                                  [p.pk for p in nearby])
            return False
        else:
            return True

    def validate_species_max(self, field, max_val, err):
        inputval = self.cleaned.get(field, None)
        if inputval:
            if max_val and inputval > max_val:
                self.append_error(err, field, max_val)
                return False

        return True

    def validate_species_dbh_max(self, species):
        return self.validate_species_max(
            fields.trees.DIAMETER,
            species.max_diameter, errors.SPECIES_DBH_TOO_HIGH)

    def validate_species_height_max(self, species):
        return self.validate_species_max(
            fields.trees.TREE_HEIGHT,
            species.max_height, errors.SPECIES_HEIGHT_TOO_HIGH)

    def validate_species(self):
        fs = fields.trees
        genus = self.datadict.get(fs.GENUS, '')
        species = self.datadict.get(fs.SPECIES, '')
        cultivar = self.datadict.get(fs.CULTIVAR, '')
        other_part = self.datadict.get(fs.OTHER_PART_OF_NAME, '')
        common_name = self.datadict.get(fs.COMMON_NAME, '')

        def append_species_error(error):
            error_fields = [genus, species, cultivar, other_part]
            error_txt = ' '.join(error_fields).strip()
            self.append_error(error, fs.SPECIES_FIELDS, error_txt)

        if genus != '' or species != '' or cultivar != '' or other_part != '':
            kwargs = {
                'instance_id': self.import_event.instance_id,
                'genus__iexact': genus,
                'species__iexact': species,
                'cultivar__iexact': cultivar,
                'other_part_of_name__iexact': other_part,
            }

            if common_name != '':
                kwargs['common_name__iexact'] = common_name
        else:
            return

        try:
            matching_species = Species.objects.get(**kwargs)
            self.cleaned[fields.trees.SPECIES_OBJECT] = matching_species
        except Species.DoesNotExist:
            append_species_error(errors.INVALID_SPECIES)
        except MultipleObjectsReturned:
            append_species_error(errors.DUPLICATE_SPECIES)

    def validate_user_defined_fields(self):
        ie = self.import_event
        for udf_def in udf_defs(ie.instance):
            column_name = ie.get_udf_column_name(udf_def)
            value = self.datadict.get(column_name, None)
            if value:
                try:
                    udf_def.clean_value(value)
                    self.cleaned[column_name] = value
                except ValidationError as ve:
                    self.append_error(
                        errors.INVALID_UDF_VALUE, column_name, str(ve))

    def validate_row(self):
        """
        Validate a row. Returns True if there were no fatal errors,
        False otherwise

        The method mutates self in two ways:
        - The 'errors' field on self will be appended to
          whenever an error is found
        - The 'cleaned' field on self will be set as fields
          get validated
        """
        # Clear errrors
        self.errors = ''

        # Convert all fields to correct datatypes
        self.validate_and_convert_datatypes()

        self.validate_user_defined_fields()

        # We can work on the 'cleaned' data from here on out
        self.validate_otm_id()

        # Attaches a GEOS point to fields.trees.POINT
        self.validate_geom()

        # This could be None or not set if there was an earlier error
        pt = self.cleaned.get(fields.trees.POINT, None)

        self.validate_species()

        # This could be None or unset if species data were not given
        species = self.cleaned.get(fields.trees.SPECIES_OBJECT, None)

        # These validations are non-fatal
        if species:
            self.validate_species_dbh_max(species)
            self.validate_species_height_max(species)

        if pt:
            self.validate_proximity(pt)

        fatal = False
        if self.has_fatal_error():
            self.status = TreeImportRow.ERROR
            fatal = True
        elif self.has_errors():  # Has 'warning'/tree watch errors
            self.status = TreeImportRow.WARNING
        else:
            self.status = TreeImportRow.VERIFIED

        self.save()
        return not fatal
