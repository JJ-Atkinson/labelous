from django.shortcuts import render
from django.http import HttpResponse, Http404
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.core.exceptions import SuspiciousOperation
from django.db import transaction

from xml.sax.saxutils import escape as xml_escape
import defusedxml.ElementTree
import types
from datetime import datetime, timezone
import secrets

from . import models
import image_mgr.models

# THEORY OF OPERATION: COMMUNICATIONS

# The LabelMe annotation tool (tool) GETs an XML document (handled by
# get_annotation_xml) that contains the current annotations for a particular
# image. When the user changes something, the tool POSTs the document back
# (handled by post_annotation_xml), and we have to update the database
# correspondingly.

# This arrangement is nice because we are assured that any particular response
# has the full and accurate state of the labels. There is no problem if a
# response gets dropped, the requests arrive in different orders, etc. The tool
# also never has to wait for a server response during editing, thus ensuring the
# tool remains responsive. Once the user closes the tool (and assuming the last
# response received was the last response that the tool sent before the close;
# the probable case), the database will accurately reflect all of the user's
# edits.

# THEORY OF OPERATION: EDIT KEYS

# There is a problem if the user has the tool open to the same annotation in two
# different tabs. Edits made in the first tab will be saved to the database, but
# they will not be displayed in the second tab. Worse, if the user then edits
# the second tab, any changes they made in the first tab will be overwritten and
# the first tab won't get updated either. To solve this, we use an "edit key".

# Whenever the current annotations are requested, the server generates a random
# number and stores it as the annotation's edit key, then transmits it to the
# tool, along with the annotations. When the tool responds with updated
# annotations, the server checks that the received edit key matches the
# annotation's stored key. If they don't match, the update is rejected and the
# tool displays an appropriate error message.

# If the user has the tool open in one tab and opens a second tab, the edit key
# will be changed. Any edits made in the second tab will be accepted because it
# has the correct key. An edit made in the first tab will no longer have the
# correct key, so it will be rejected. The first tab will then tell the user
# that the annotation open elsewhere and lock itself to prevent further edits.

# Careful readers will note that this is very similar to the operation of CSRF
# tokens. Currently, the edit key cannot be used as one because it is generated
# by a GET request. Additionally, since it is generated by a GET request, an
# "attacker" could use CSRF to annoy the user by invalidating all the tools they
# currently have open.

# THEORY OF OPERATION: UPDATING THE DATABASE

# When the tool requests the annotations, each polygon returned knows its
# database ID. If the tool edits one of those polygons, the server can simply
# look up the corresponding database record and apply the changes. However,
# polygons newly created by the tool don't have database IDs, and we don't
# assign them one because it would require asking the server. Fortunately, the
# tool does not change the order of the polygons within the received annotation
# file, and always adds new polygons to the end. However, the order will change
# each time the tool loads the annotations.

# To correctly update the polygons, we track each polygon's annotation file
# index in the database. Whenever the annotations are requested, we randomize
# the edit key, clear the indices from all the polygons, then send the polygons
# we have along with their database IDs. If the tool sends back a polygon that
# has a database ID, we look up its database record by the database ID and
# simply ignore the index.

# Otherwise, we must look up the polygon by its index. If we can't find
# anything, we assume the polygon is new and create a database record for it,
# which includes its index. We are assured the polygon is new because indices
# are unique (for a particular request), and we cleared the indices when the
# annotations were requested, so the new polygon's index couldn't have matched
# an old polygon with the same index.

# If we find a polygon with that index, we edit it as usual. We are assured we
# have found the correct polygon because we created a record with its index when
# it was new, indices don't change after the annotations are requested, and we
# will reject any updates that don't have the edit key (and thus indices) of the
# most recent annotation request.


def get_annotation_xml(request, image_id):
    # regex only allows numbers through
    image_id = int(image_id)

    try:
        image = image_mgr.models.Image.objects.get(pk=image_id)
        exists = True
    except image_mgr.models.Image.DoesNotExist:
        exists = False
        # we don't check for if multiple exist, because if that happens,
        # something has gone horrifically wrong in the database.

    if image.visible is False:
        exists = False
    else:
        try:
            annotation = models.Annotation.objects.get(
                annotator=request.user, image=image, deleted=False)
        except models.Annotation.DoesNotExist:
            exists = False
            # if multiple un-deleted annotations exist, something has gone
            # terribly wrong.

    if not exists:
        raise Http404("Annotation does not exist.")

    # randomize the edit token. we don't use a transaction here because it's the
    # annotation update code's responsibility to make sure it doesn't commit
    # any data when the edit key is incorrect.
    edit_key = secrets.token_bytes(16)
    annotation.edit_key = edit_key
    annotation.save()

    # find all the visible polygons attached to this annotation
    polygons = annotation.polygons.filter(deleted=False)
    # and remove any old indices, ensuring the database only contains indices
    # for the file we are about to build
    polygons.filter(anno_index__isnull=False).update(anno_index=None)

    # because XML is hard and bad, we build the result with string operations.
    xml = ["<annotation>"]
    # the annotation tool doesn't rebuild the document, it only modifies it.
    # this means we can attach arbitrary tags (which we prefix with c_) and they
    # will be returned untouched. it also means that most of the formatting we
    # apply will be preserved. formatting is wasted bytes, so we don't put it
    # in.

    # write which annotation ID this represents. we don't get anything other
    # than the xml when this document is returned, so we need it to look back up
    # where it came from.
    xml.append("<c_anno_id>{}</c_anno_id>".format(annotation.pk))
    # store the edit key as a hex string. this, basically, ensures that the user
    # doesn't get confused by having the same annotation open multiple times,
    # and that the file's structure still matches the database.
    xml.append("<edit_key>{}</edit_key>".format(edit_key.hex()))
    # specify which image file to show for this annotation. since we look up
    # images by their ID, the folder doesn't matter as long as it's constant.
    xml.append("<filename>img{}.jpg</filename><folder>f</folder>".format(
        image_id))
    for polygon in polygons:
        xml.append("<object>")
        # we need to know the polygon ID so we can update the record if the user
        # changed the points
        xml.append("<c_poly_id>{}</c_poly_id>".format(polygon.pk))
        # the polygon's label as text
        xml.append("<name>{}</name>".format(xml_escape(polygon.label_as_str)))
        # if deleted is 1, the polygon won't show up. we avoid sending deleted
        # polygons, so there's no case it would be set to 1.
        # if verified is 1, the polygon will show an error if the user tries to
        # edit it. we map this to the polygon's locked status.
        xml.append("<deleted>0</deleted><verified>{}</verified>".format(
            1 if polygon.locked or annotation.locked else 0))
        # whether the user considers the polygon to be occluded. same as
        # database flag.
        xml.append("<occluded>{}</occluded>".format(
            "yes" if polygon.occluded else "no"))
        # any additional notes the user wants to put.
        xml.append("<attributes>{}</attributes>".format(
            xml_escape(polygon.notes)))
        # now the actual polygon points. we need to specify the user that
        # created the polygon. so the tool is happy, we claim this is always the
        # logged in user (or for now, a constant user.)
        xml.append("<polygon><username>hi</username>")
        points = polygon.points
        # points are stored as a flat array: even indices are x and odd are y
        for pi in range(0, len(points), 2):
            # the tool can send non-integer coordinates even if they are a
            # little silly. we specify a limit of 2 decimal places to get good
            # accuracy and make sure the numbers are reasonable length.
            # (i.e. not 3.5000000000000000069 or w/e)
            xml.append("<pt><x>{:.2f}</x><y>{:.2f}</y></pt>".format(
                points[pi], points[pi+1]))
        xml.append("</polygon></object>")
    xml.append("</annotation>")

    # django will automatically concatenate our xml strings
    return HttpResponse(xml, content_type="text/xml")


# handle a returned annotation XML document. note that we get no additional
# information, just what's inside the xml. also note that we have to be
# intensely suspicious of its contents, because the labeling tool performs no
# validation of any kind at all whatsoever (and any whacko could POST whatever
# XML they wanted to anyway). if anything is weird, we throw a
# SuspiciousOperation exception, which causes the tool to reload and get the
# correct annotations back from the database.

# handle understanding the document and updating the database. if this raises
# any kind of exception, the database transaction is rolled back.
def process_annotation_xml(request, root):
    if root.tag != "annotation":
        raise SuspiciousOperation("not an annotation")

    # look up the image we are allegedly annotating
    try:
        filename = root.find("filename").text
        if not filename.startswith("img") or not filename.endswith(".jpg"):
            raise Exception("invalid filename {}".format(filename))
        image_id = int(filename[3:-4])
        image = image_mgr.models.Image.objects.get(pk=image_id)
        if not image.visible:
            raise Exception("invisible image")
    except Exception as e:
        raise SuspiciousOperation("invalid image") from e

    # look up the annotation this document is allegedly for
    try:
        anno_id = int(root.find("c_anno_id").text)
        annotation = models.Annotation.objects.get(
            annotator=request.user, image=image, locked=False, deleted=False)
        if annotation.deleted:
            raise Exception("deleted annotation")
    except Exception as e:
        raise SuspiciousOperation("invalid anno id") from e

    # make sure the edit key matches. note that this is optimistic; it could be
    # changed out from under us while we are processing. we re-check right
    # before committing the data, but checking here saves processing in the
    # common case.
    try:
        edit_key = bytes.fromhex(root.find("edit_key").text)
        if edit_key != bytes(annotation.edit_key):
            raise Exception("edit key does not match")
    except Exception as e:
        # probably will be modified in the future; a bad or missing edit key
        # isn't necessarily suspicious
        raise SuspiciousOperation("invalid edit key") from e

    # pull out all the polygons defined in this document. once that is done, we
    # will apply them to the database.
    anno_polygons = []
    anno_poly_ids = set()
    # iterate through objects in order, keeping track of their index
    curr_index = 0
    for obj_tag in root.findall("object"):
        anno_polygon = types.SimpleNamespace()
        try:
            # newly-created polygons won't have IDs
            if obj_tag.find("c_poly_id") is None:
                anno_polygon.id = None
            else:
                # it has an ID and it must be correct
                anno_polygon.id = int(obj_tag.find("c_poly_id").text)
                if anno_polygon.id in anno_poly_ids:
                    raise Exception("duplicate id")
                anno_poly_ids.add(anno_polygon.id)

            anno_polygon.index = curr_index
            curr_index = curr_index + 1

            anno_polygon.name = obj_tag.find("name").text
            if anno_polygon.name == "":
                raise Exception("empty name")

            deleted = int(obj_tag.find("deleted").text)
            if deleted not in (0, 1):
                raise Exception("bad deleted")
            anno_polygon.deleted = False if deleted == 0 else True

            occluded = obj_tag.find("occluded").text
            if occluded not in ("no", "yes"):
                raise Exception("bad occluded")
            anno_polygon.occluded = False if occluded == "no" else True

            try:
                attributes = obj_tag.find("attributes").text
            except:
                attributes = None
            anno_polygon.attributes = "" if attributes == None else attributes

            anno_polygon.points = []
            try:
                for point_tag in obj_tag.find("polygon").findall("pt"):
                    # this double-conversion makes sure the numbers are received
                    # with the same precision we send them, and thus avoids
                    # problems where the number didn't change but isn't quite
                    # equal to what the database has.
                    x = float("{:.2f}".format(float(point_tag.find("x").text)))
                    y = float("{:.2f}".format(float(point_tag.find("y").text)))
                    anno_polygon.points.extend((x, y))
            except:
                raise Exception("bad points")

            anno_polygons.append(anno_polygon)
        except Exception as e:
            raise SuspiciousOperation("invalid polygon") from e

    # get the polygons attached to this annotation that we would have shown
    polygons = annotation.polygons.filter(deleted=False)
    # and map them by their ID
    polygons_by_id = {p.pk: p for p in polygons}
    # plus index in the file
    polygons_by_index = {p.anno_index: p
        for p in polygons_by_id.values() if p.anno_index is not None}
    with transaction.atomic():
        # reload the annotation, this time while selected for update. this
        # ensures that nobody else can change it until the transaction finishes.
        annotation = models.Annotation.objects.select_for_update().get(
            pk=annotation.pk)
        # now we can be sure the edit key is correct
        if edit_key != bytes(annotation.edit_key):
            raise SuspiciousOperation("invalid edit key")
        # the edit key can't be changed until the transaction finishes, ensuring
        # that any changes are in the database before a new edit can happen.

        # mesaure if anything changed in the annotation so we can update its
        # last edited time.
        annotation_changed = False
        for anno_poly in anno_polygons:
            # mesaure if anything changed in the polygon so we can update its
            # last edited time.
            polygon_changed = False

            if anno_poly.id is not None:
                try:
                    poly = polygons_by_id[anno_poly.id]
                except KeyError:
                    # if it was deleted, we wouldn't have loaded it
                    if anno_poly.deleted:
                        continue
                    else:
                        raise
            else:
                try:
                    # if it was created under this edit key, we need to find it
                    # by index instead
                    poly = polygons_by_index[anno_poly.index]
                except KeyError:
                    # if we can't find it by index, it must be new
                    # (or deleted, and we didn't load it)
                    if anno_poly.deleted:
                        continue
                    else:
                        poly = models.Polygon(
                            annotation=annotation, anno_index=anno_poly.index)
                        polygon_changed = True

            if polygon_changed == True: pass
            elif poly.label_as_str != anno_poly.name: polygon_changed = True
            elif poly.notes != anno_poly.attributes: polygon_changed = True
            elif poly.occluded != anno_poly.occluded: polygon_changed = True
            elif poly.points != anno_poly.points: polygon_changed = True
            elif poly.deleted != anno_poly.deleted: polygon_changed = True
            if polygon_changed: print("poly changed!!")

            if polygon_changed:
                poly.label_as_str = anno_poly.name
                poly.notes = anno_poly.attributes
                poly.occluded = anno_poly.occluded
                poly.points = anno_poly.points
                poly.deleted = anno_poly.deleted
                poly.last_edit_time = datetime.now(timezone.utc)
                poly.save()
                annotation_changed = True

        if annotation_changed:
            annotation.last_edit_time = datetime.now(timezone.utc)
            annotation.save(update_fields=('last_edit_time',))


# parse the XML data. the request can't be, by default, bigger than 2.5MiB, so
# it shouldn't consume too much memory. the options given to parse prevent
# expansion attacks and external sourcing garbage.
def parse_annotation_xml(request):
    try:
        # empirically, the request seems to be utf8
        xml = defusedxml.ElementTree.fromstring(request.body.decode("utf8"),
            forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except Exception as e:
        raise SuspiciousOperation("xml parse failed") from e

    try:
        process_annotation_xml(request, xml)
    except SuspiciousOperation:
        raise
    except Exception as e:
        raise SuspiciousOperation("xml process failed") from e

# DANGER!!!! CSRF should be used to prevent forged annotations from being
# uploaded. but that would require hacking labelme to properly transmit the
# token. apparently django stores it in a cookie so this could be done later.
@csrf_exempt
def post_annotation_xml(request):
    try:
        parse_annotation_xml(request)
    except Exception:
        import traceback
        traceback.print_exc()
        raise

    # since the data was posted by XHR, we are expected to reply with SOME kind
    # of XML. what that is doesn't matter. eventually it will be quasi-related
    # to any error so the tool can take appropriate action.
    return HttpResponse("<nop/>", content_type="text/xml")
