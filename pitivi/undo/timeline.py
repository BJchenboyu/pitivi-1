# Pitivi video editor
#
#       pitivi/undo/timeline.py
#
# Copyright (c) 2009, Alessandro Decina <alessandro.d@gmail.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin St, Fifth Floor,
# Boston, MA 02110-1301, USA.
from gi.repository import GES
from gi.repository import GObject
from gi.repository import Gst

from pitivi.effects import PROPS_TO_IGNORE
from pitivi.undo.undo import FinalizingAction
from pitivi.undo.undo import PropertyChangeTracker
from pitivi.undo.undo import UndoableAction
from pitivi.utils.loggable import Loggable


def child_property_name(pspec):
    return "%s::%s" % (pspec.owner_type.name, pspec.name)


class CommitTimelineFinalizingAction(FinalizingAction):
    def __init__(self, pipeline):
        self.__pipeline = pipeline

    def do(self):
        self.__pipeline.commit_timeline()


class TrackElementPropertyChanged(UndoableAction):

    def __init__(self, track_element, property_name, old_value, new_value):
        UndoableAction.__init__(self)
        self.track_element = track_element
        self.property_name = property_name
        self.old_value = old_value
        self.new_value = new_value

    def do(self):
        self.track_element.set_child_property(
            self.property_name, self.new_value)
        self._done()

    def undo(self):
        self.track_element.set_child_property(
            self.property_name, self.old_value)
        self._undone()

    def asScenarioAction(self):
        st = Gst.Structure.new_empty("set-child-property")
        st['element-name'] = self.track_element.get_name()
        st['property'] = self.property_name
        value = self.new_value
        if isinstance(self.new_value, GObject.GFlags) or\
                isinstance(self.new_value, GObject.GEnum):
            value = int(self.new_value)
        st['value'] = value

        return st


# FIXME We should refactor pitivi.undo.PropertyChangeTracker so we can use it as
# a baseclass here!
class TrackElementChildPropertyTracker(Loggable):

    """
    Track track_element configuration changes in its list of control track_elements
    """

    def __init__(self, action_log):
        Loggable.__init__(self)
        self._tracked_track_elements = {}
        self.action_log = action_log

    def addTrackElement(self, track_element):
        if track_element in self._tracked_track_elements:
            return

        if track_element.get_track() is None:
            track_element.connect(
                "notify::track", self._trackElementTrackSetCb)
            return

        self._discoverChildProperties(track_element)

    def _trackElementTrackSetCb(self, track_element, unused):
        self._discoverChildProperties(track_element)
        track_element.disconnect_by_func(self._trackElementTrackSetCb)

    def _discoverChildProperties(self, track_element):
        properties = {}

        track_element.connect('deep-notify', self._propertyChangedCb)

        for prop in track_element.list_children_properties():
            if prop.name in PROPS_TO_IGNORE:
                continue

            prop_name = child_property_name(prop)
            properties[prop_name] = track_element.get_child_property(prop_name)[1]

        self._tracked_track_elements[track_element] = properties

    def getPropChangedFromTrackElement(self, track_element):
        return self._tracked_track_elements[track_element]

    def _propertyChangedCb(self, track_element, unused_gstelement, pspec):

        pspec_name = child_property_name(pspec)
        if track_element.get_control_binding(pspec_name):
            self.debug("Property %s controlled", pspec_name)
            return

        old_value = self._tracked_track_elements[track_element][pspec_name]
        new_value = track_element.get_child_property(pspec_name)[1]
        action = TrackElementPropertyChanged(
            track_element, pspec_name, old_value, new_value)
        self._tracked_track_elements[track_element][pspec_name] = new_value
        self.action_log.push(action)


class TrackElementAdded(UndoableAction):
    # Note: We have a bug if we just remove the Effect from the timeline
    # and keep it saved here and then readd it to corresponding timeline (it
    # freezes everything). So what we are doing is  to free the Effect,
    # keep its settings here when undoing, and instanciate a new one when
    # doing again. We have to keep all EffectPropertyChanged object that refers
    # to the Effect when undoing so we reset theirs track_element when
    # doing it again. The way of doing it is the same with EffectRemoved

    def __init__(self, clip, track_element, properties_watcher):
        UndoableAction.__init__(self)
        self.clip = clip
        self.track_element = track_element
        self.asset = track_element.get_asset()
        self.track_element_props = []
        self.gnl_obj_props = []
        self._properties_watcher = properties_watcher
        self._props_changed = []

    def do(self):
        self.track_element = self.clip.add_asset(self.asset)
        for prop_name, prop_value in self.track_element_props:
            self.track_element.set_child_property(prop_name, prop_value)
        self._props_changed = []
        self._done()

    def undo(self):
        props = self.track_element.list_children_properties()
        self.track_element_props = [(child_property_name(prop), self.track_element.get_child_property(child_property_name(prop))[1])
                                    for prop in props
                                    if prop.flags & GObject.PARAM_WRITABLE and prop.name not in PROPS_TO_IGNORE]
        self.clip.remove(self.track_element)
        self._props_changed =\
            self._properties_watcher.getPropChangedFromTrackElement(
                self.track_element)
        del self.track_element
        self.track_element = None
        self._undone()

    def asScenarioAction(self):
        st = Gst.Structure.new_empty("container-add-child")
        st["container-name"] = self.clip.get_name()
        st["asset-id"] = self.track_element.get_id()
        st["child-type"] = GObject.type_name(
            self.track_element.get_asset().get_extractable_type())

        return st


class TrackElementRemoved(UndoableAction):

    def __init__(self, clip, track_element, properties_watcher):
        UndoableAction.__init__(self)
        self.track_element = track_element
        self.clip = clip
        self.asset = track_element.get_asset()
        self.track_element_props = []
        self.gnl_obj_props = []
        self._properties_watcher = properties_watcher
        self._props_changed = []

    def do(self):
        props = self.track_element.list_children_properties()
        self.track_element_props = [(child_property_name(prop), self.track_element.get_child_property(child_property_name(prop))[1])
                                    for prop in props
                                    if prop.flags & GObject.PARAM_WRITABLE and prop.name not in PROPS_TO_IGNORE]

        self.clip.remove(self.track_element)

        self._props_changed =\
            self._properties_watcher.getPropChangedFromTrackElement(
                self.track_element)
        del self.track_element
        self.track_element = None
        self._done()

    def undo(self):
        self.track_element = self.clip.add_asset(self.asset)
        for prop_name, prop_value in self.track_element_props:
            self.track_element.set_child_property(prop_name, prop_value)
        self._props_changed = []
        self._undone()

    def asScenarioAction(self):
        st = Gst.Structure.new_empty("container-remove-child")
        st["container-name"] = self.clip.get_name()
        st["child-name"] = self.track_element.get_name()

        return st


class ClipPropertyChangeTracker(PropertyChangeTracker):

    def __init__(self, ges_clip):
        PropertyChangeTracker.__init__(self, ges_clip,
            # No out-point as that's calculated.
            property_names=["start", "duration", "in-point", "priority"])
        self.timeline = ges_clip.timeline
        self.timeline.connect("commited", self._timelineCommitedCb)

    def disconnectFromObject(self, obj):
        self.timeline.disconnect_by_func(self._timelineCommitedCb)
        PropertyChangeTracker.disconnectFromObject(self, obj)

    def _timelineCommitedCb(self, timeline):
        properties = self._takeCurrentSnapshot(self.gobject)
        for property_name, property_value in properties.items():
            old_value = self.properties[property_name]
            if old_value != property_value:
                self._propertyChangedCb(
                    self.gobject, property_value, property_name)


class KeyframeChangeTracker(GObject.Object):

    __gsignals__ = {
        "keyframe-moved": (GObject.SIGNAL_RUN_LAST, None, (object, object, object, object)),
    }

    def __init__(self):
        GObject.Object.__init__(self)
        self.keyframes = None
        self.control_source = None

    def connectToObject(self, control_source):
        self.control_source = control_source
        self.keyframes = self._takeCurrentSnapshot(control_source)
        control_source.connect("value-added", self._keyframeAddedCb)
        control_source.connect("value-removed", self._keyframeRemovedCb)
        control_source.connect("value-changed", self._keyframeMovedCb)

    def _takeCurrentSnapshot(self, control_source):
        keyframes = {}
        for keyframe in self.control_source.get_all():
            keyframes[keyframe.timestamp] = self._getKeyframeSnapshot(keyframe)

        return keyframes

    def disconnectFromObject(self, control_source):
        self.control_source = None
        control_source.disconnect_by_func(self._keyframeMovedCb)

    def _keyframeAddedCb(self, control_source, keyframe):
        self.keyframes[keyframe.timestamp] = self._getKeyframeSnapshot(keyframe)

    def _keyframeRemovedCb(self, control_source, keyframe):
        del self.keyframes[keyframe.timestamp]

    def _keyframeMovedCb(self, control_source, keyframe):
        old_snapshot = self.keyframes[keyframe.timestamp]
        new_snapshot = self._getKeyframeSnapshot(keyframe)
        self.keyframes[keyframe.timestamp] = new_snapshot

        self.emit("keyframe-moved", control_source,
                  keyframe, old_snapshot, new_snapshot)

    def _getKeyframeSnapshot(self, keyframe):
        return (keyframe.timestamp, keyframe.value)


class ClipPropertyChanged(UndoableAction):

    def __init__(self, clip, property_name, old_value, new_value):
        UndoableAction.__init__(self)
        self.clip = clip
        self.property_name = property_name
        self.old_value = old_value
        self.new_value = new_value

    def do(self):
        self.clip.set_property(
            self.property_name.replace("-", "_"), self.new_value)
        self._done()

    def undo(self):
        self.clip.set_property(
            self.property_name.replace("-", "_"), self.old_value)
        self._undone()


class ClipAdded(UndoableAction):

    def __init__(self, layer, clip):
        UndoableAction.__init__(self)
        self.layer = layer
        self.clip = clip

    def do(self):
        self.clip.set_name(None)
        self.layer.add_clip(self.clip)
        self.layer.get_timeline().get_asset().pipeline.commit_timeline()
        self._done()

    def undo(self):
        self.layer.remove_clip(self.clip)
        self.layer.get_timeline().get_asset().pipeline.commit_timeline()
        self._undone()

    def asScenarioAction(self):
        timeline = self.layer.get_timeline()
        if hasattr(self.layer, "splitting_object") and \
                self.layer.splitting_object is True:
            return None
        elif hasattr(timeline, "ui") and timeline.ui and timeline.ui.editing_context is not None:
            return None

        st = Gst.Structure.new_empty("add-clip")
        st.set_value("name", self.clip.get_name())
        st.set_value("layer-priority", self.layer.props.priority)
        st.set_value("asset-id", self.clip.get_asset().get_id())
        st.set_value("type", GObject.type_name(self.clip))
        st.set_value("start", float(self.clip.props.start / Gst.SECOND))
        st.set_value("inpoint", float(self.clip.props.in_point / Gst.SECOND))
        st.set_value("duration", float(self.clip.props.duration / Gst.SECOND))
        return st


class ClipRemoved(UndoableAction):

    def __init__(self, layer, clip):
        UndoableAction.__init__(self)
        self.layer = layer
        self.clip = clip

    def do(self):
        self.layer.remove_clip(self.clip)
        self.layer.get_timeline().get_asset().pipeline.commit_timeline()
        self._done()

    def undo(self):
        self.clip.set_name(None)
        self.layer.add_clip(self.clip)
        self.layer.get_timeline().get_asset().pipeline.commit_timeline()
        self._undone()

    def asScenarioAction(self):
        timeline = self.layer.get_timeline()
        if hasattr(timeline, "ui") and timeline.ui\
                and timeline.ui.editing_context is not None:
            return None

        st = Gst.Structure.new_empty("remove-clip")
        st.set_value("name", self.clip.get_name())
        return st


class LayerAdded(UndoableAction):

    def __init__(self, ges_timeline, ges_layer):
        UndoableAction.__init__(self)
        self.ges_timeline = ges_timeline
        self.ges_layer = ges_layer

    def do(self):
        self.ges_timeline.add_layer(self.ges_layer)

    def undo(self):
        self.ges_timeline.remove_layer(self.ges_layer)
        self.ges_timeline.get_asset().pipeline.commit_timeline()

    def asScenarioAction(self):
        st = Gst.Structure.new_empty("add-layer")
        st.set_value("priority", self.ges_layer.props.priority)
        st.set_value("auto-transition", self.ges_layer.props.auto_transition)
        return st


class LayerRemoved(UndoableAction):

    def __init__(self, ges_timeline, ges_layer):
        UndoableAction.__init__(self)
        self.ges_timeline = ges_timeline
        self.ges_layer = ges_layer

    def do(self):
        self.ges_timeline.remove_layer(self.ges_layer)
        self.ges_timeline.get_asset().pipeline.commit_timeline()

    def undo(self):
        self.ges_timeline.add_layer(self.ges_layer)

    def asScenarioAction(self):
        st = Gst.Structure.new_empty("remove-layer")
        st.set_value("priority", self.ges_layer.props.priority)
        return st


class LayerMoved(UndoableAction):

    def __init__(self, ges_layer, old_priority, priority):
        UndoableAction.__init__(self)
        self.ges_layer = ges_layer
        self.old_priority = old_priority
        self.priority = priority

    def do(self):
        self.ges_layer.props.priority = self.priority

    def undo(self):
        self.ges_layer.props.priority = self.old_priority

    def asScenarioAction(self):
        st = Gst.Structure.new_empty("move-layer")
        st.set_value("priority", self.ges_layer.props.priority)
        return st


class ControlSourceValueAdded(UndoableAction):

    def __init__(self, track_element,
                 control_source, keyframe,
                 property_name):
        UndoableAction.__init__(self)
        self.control_source = control_source
        self.keyframe = keyframe
        self.property_name = property_name
        self.track_element = track_element

    def do(self):
        self.control_source.set(self.keyframe.timestamp,
                                self.keyframe.value)
        self._done()

    def undo(self):
        self.control_source.unset(self.keyframe.timestamp)
        self._undone()

    def asScenarioAction(self):
        st = Gst.Structure.new_empty("add-keyframe")
        st.set_value("element-name", self.track_element.get_name())
        st.set_value("property-name", self.property_name)
        st.set_value("timestamp", float(self.keyframe.timestamp / Gst.SECOND))
        st.set_value("value", self.keyframe.value)
        return st


class ControlSourceValueRemoved(UndoableAction):

    def __init__(self, track_element,
                 control_source, keyframe, property_name):
        UndoableAction.__init__(self)
        self.control_source = control_source
        self.keyframe = keyframe
        self.property_name = property_name
        self.track_element = track_element

    def do(self):
        self.control_source.unset(self.keyframe.timestamp)
        self._undone()

    def undo(self):
        self.control_source.set(self.keyframe.timestamp,
                                self.keyframe.value)
        self._done()

    def asScenarioAction(self):
        st = Gst.Structure.new_empty("remove-keyframe")
        st.set_value("element-name", self.track_element.get_name())
        st.set_value("property-name", self.property_name)
        st.set_value("timestamp", float(self.keyframe.timestamp / Gst.SECOND))
        st.set_value("value", self.keyframe.value)
        return st


class ControlSourceKeyframeChanged(UndoableAction):

    def __init__(self, control_source, old_snapshot, new_snapshot):
        UndoableAction.__init__(self)
        self.control_source = control_source
        self.old_snapshot = old_snapshot
        self.new_snapshot = new_snapshot

    def do(self):
        self._applySnapshot(self.new_snapshot)
        self._done()

    def undo(self):
        self._applySnapshot(self.old_snapshot)
        self._undone()

    def _applySnapshot(self, snapshot):
        time, value = snapshot
        self.control_source.set(time, value)


class ActivePropertyChanged(UndoableAction):

    def __init__(self, effect_action, active):
        UndoableAction.__init__(self)
        self.effect_action = effect_action
        self.active = not active

    def do(self):
        self.effect_action.track_element.active = self.active
        self.active = not self.active
        self._done()

    def undo(self):
        self.effect_action.track_element.active = self.active
        self.active = not self.active
        self._undone()


class TimelineObserver(Loggable):
    """Monitors a project's timeline and reports UndoableActions.

    Attributes:
        action_log (UndoableActionLog): The action log where to report actions.
    """

    timelinePropertyChangedAction = ClipPropertyChanged
    activePropertyChangedAction = ActivePropertyChanged

    def __init__(self, action_log, app):
        Loggable.__init__(self)
        self.action_log = action_log
        self.app = app
        self.clip_property_trackers = {}
        self.control_source_keyframe_trackers = {}
        self.children_props_tracker = TrackElementChildPropertyTracker(self.action_log)
        self._layers_priorities = {}

    def startObserving(self, ges_timeline):
        """Starts monitoring the specified timeline.

        Args:
            ges_timeline (GES.Timeline): The timeline to be monitored.
        """
        for ges_layer in ges_timeline.get_layers():
            self._connect_to_layer(ges_layer)

        ges_timeline.connect("layer-added", self._layerAddedCb)
        ges_timeline.connect("layer-removed", self._layerRemovedCb)

        for ges_layer in ges_timeline.get_layers():
            for ges_clip in ges_layer.get_clips():
                self._connectToClip(ges_clip)

    def _connect_to_layer(self, ges_layer):
        self._layers_priorities[ges_layer] = ges_layer.props.priority
        ges_layer.connect("clip-added", self._clipAddedCb)
        ges_layer.connect("clip-removed", self._clipRemovedCb)
        ges_layer.connect("notify::priority", self._layer_moved_cb)

    def _disconnect_from_layer(self, ges_layer):
        del self._layers_priorities[ges_layer]
        ges_layer.disconnect_by_func(self._clipAddedCb)
        ges_layer.disconnect_by_func(self._clipRemovedCb)
        ges_layer.disconnect_by_func(self._layer_moved_cb)

    def _connectToClip(self, ges_clip):
        tracker = ClipPropertyChangeTracker(ges_clip)
        for property_name in tracker.property_names:
            attr_name = "last-%s" % property_name
            last_value = ges_clip.get_property(property_name)
            setattr(tracker, attr_name, last_value)
        tracker.connect(
            "monitored-property-changed", self._clipPropertyChangedCb)
        self.clip_property_trackers[ges_clip] = tracker

        ges_clip.connect("child-added", self._clipTrackElementAddedCb)
        ges_clip.connect("child-removed", self._clipTrackElementRemovedCb)
        for track_element in ges_clip.get_children(True):
            self._connectToTrackElement(track_element)

    def _disconnectFromClip(self, clip):
        if isinstance(clip, GES.TransitionClip):
            return

        for child in clip.get_children(True):
            self._disconnectFromTrackElement(child)

        clip.disconnect_by_func(self._clipTrackElementAddedCb)
        clip.disconnect_by_func(self._clipTrackElementRemovedCb)
        tracker = self.clip_property_trackers.pop(clip)
        tracker.disconnectFromObject(clip)
        tracker.disconnect_by_func(self._clipPropertyChangedCb)

    def _controlBindingAddedCb(self, track_element, binding):
        self._connectToControlSource(track_element, binding)

    def _connectToTrackElement(self, track_element):
        for prop, binding in track_element.get_all_control_bindings().items():
            self._connectToControlSource(track_element, binding,
                                         existed=True)
        track_element.connect("control-binding-added",
                              self._controlBindingAddedCb)
        if isinstance(track_element, GES.BaseEffect):
            self.children_props_tracker.addTrackElement(track_element)
        elif isinstance(track_element, GES.VideoSource):
            self.children_props_tracker.addTrackElement(track_element)

    def _disconnectFromTrackElement(self, track_element):
        for prop, binding in track_element.get_all_control_bindings().items():
            self._disconnectFromControlSource(binding)

    def _connectToControlSource(self, track_element, binding, existed=False):
        control_source = binding.props.control_source

        control_source.connect("value-added",
                               self._controlSourceKeyFrameAddedCb,
                               track_element,
                               binding.props.name)

        control_source.connect("value-removed",
                               self._controlSourceKeyFrameRemovedCb,
                               track_element,
                               binding.props.name)

        tracker = KeyframeChangeTracker()
        tracker.connectToObject(control_source)
        tracker.connect("keyframe-moved", self._controlSourceKeyFrameMovedCb)
        self.control_source_keyframe_trackers[control_source] = tracker

        if not existed:
            self.app.write_action("set-control-source",
                                  {"element-name": track_element.get_name(),
                                   "property-name": binding.props.name,
                                   "binding-type": "direct",
                                   "source-type": "interpolation",
                                   "interpolation-mode": "linear"})

    def _disconnectFromControlSource(self, binding):
        control_source = binding.props.control_source

        try:
            control_source.disconnect_by_func(self._controlSourceKeyFrameAddedCb)
            control_source.disconnect_by_func(self._controlSourceKeyFrameRemovedCb)
        except TypeError:
            pass

        try:
            tracker = self.control_source_keyframe_trackers.pop(control_source)
            tracker.disconnectFromObject(control_source)
            tracker.disconnect_by_func(self._controlSourceKeyFrameMovedCb)
        except KeyError:
            self.debug("Control source already disconnected: %s" % control_source)
            pass

    def _clipAddedCb(self, layer, clip):
        if isinstance(clip, GES.TransitionClip):
            return
        self._connectToClip(clip)
        action = ClipAdded(layer, clip)
        self.action_log.push(action)

    def _clipRemovedCb(self, layer, clip):
        if isinstance(clip, GES.TransitionClip):
            return
        self._disconnectFromClip(clip)
        action = ClipRemoved(layer, clip)
        self.action_log.push(action)

    def _clipPropertyChangedCb(self, tracker, clip,
                               property_name, old_value, new_value):

        # Do not track clips when we are just moving clips around
        # while drag and dropping them.
        if self.app.gui.timeline_ui.timeline.dropping_clips:
            return

        attr_name = "last-%s" % property_name
        new_value = clip.get_property(property_name)
        old_value = getattr(tracker, attr_name)
        action = self.timelinePropertyChangedAction(clip, property_name,
                                                    old_value, new_value)
        setattr(tracker, attr_name, new_value)
        self.action_log.push(action)

    def _clipTrackElementAddedCb(self, clip, track_element):
        self._connectToTrackElement(track_element)
        if isinstance(track_element, GES.BaseEffect):
            action = TrackElementAdded(clip, track_element,
                                       self.children_props_tracker)
            self.action_log.push(action)

    def _clipTrackElementRemovedCb(self, clip, track_element):
        self.debug("%s REMOVED from (%s)" % (track_element, clip))
        self._disconnectFromTrackElement(track_element)
        if isinstance(track_element, GES.BaseEffect):
            action = TrackElementRemoved(clip, track_element,
                                         self.children_props_tracker)
            self.action_log.push(action)

    def _controlSourceKeyFrameAddedCb(self, source, keyframe, track_element,
                                      property_name):
        action = ControlSourceValueAdded(track_element,
                                         source, keyframe, property_name)
        self.action_log.push(action)

    def _controlSourceKeyFrameRemovedCb(self, source, keyframe, track_element,
                                        property_name):
        action = ControlSourceValueRemoved(track_element,
                                           source, keyframe, property_name)
        self.action_log.push(action)

    def _trackElementActiveChangedCb(self, track_element, active, add_effect_action):
        """
        This happens when an effect is (de)activated on a clip in the timeline.
        """
        action = self.activePropertyChangedAction(add_effect_action, active)
        self.action_log.push(action)

    def _controlSourceKeyFrameMovedCb(self, tracker, control_source,
                                      keyframe, old_snapshot, new_snapshot):
        action = ControlSourceKeyframeChanged(control_source,
                                              old_snapshot, new_snapshot)
        self.action_log.push(action)

    def _layer_moved_cb(self, ges_layer, unused_param):
        previous = self._layers_priorities[ges_layer]
        current = ges_layer.props.priority
        self._layers_priorities[ges_layer] = current
        action = LayerMoved(ges_layer, previous, current)
        self.action_log.push(action)

    def _layerAddedCb(self, ges_timeline, ges_layer):
        self._connect_to_layer(ges_layer)
        action = LayerAdded(ges_timeline, ges_layer)
        self.action_log.push(action)

    def _layerRemovedCb(self, ges_timeline, ges_layer):
        self._disconnect_from_layer(ges_layer)
        action = LayerRemoved(ges_timeline, ges_layer)
        self.action_log.push(action)
