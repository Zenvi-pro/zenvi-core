/**
 * @file
 * @brief Keyframe directive (draggable keyframes on the timeline)
 */

/*global App, findElement, uuidv4, snapToFPSGridTime, pixelToTime, timeline, angular*/
App.directive("tlKeyframe", function () {
  return {
    link: function (scope, element, attrs) {
      var obj, objType = attrs.objectType, objId = attrs.objectId;
      var fps = scope.project.fps.num / scope.project.fps.den;
      var transactionId = null;
      var currentFrame = parseInt(attrs.point, 10);

      function toNumber(value, fallback) {
        var parsed = parseFloat(value);
        return isNaN(parsed) ? fallback : parsed;
      }

      function locateObject() {
        if (objType === "clip") {
          obj = findElement(scope.project.clips, "id", objId);
        } else {
          obj = findElement(scope.project.effects, "id", objId);
        }
      }

      function getBounds(object) {
        if (!object) { return null; }
        var start = toNumber(object.start, 0);
        var end = toNumber(object.end, NaN);
        if (isNaN(end)) {
          var duration = toNumber(object.duration, NaN);
          if (!isNaN(duration)) {
            end = start + duration;
          } else {
            end = start;
          }
        }
        start = snapToFPSGridTime(scope, start);
        end = snapToFPSGridTime(scope, end);
        if (end < start) {
          var temp = start;
          start = end;
          end = temp;
        }
        return {start: start, end: end};
      }

      function clampSeconds(object, seconds) {
        var bounds = getBounds(object);
        if (!bounds) { return seconds; }
        if (seconds < bounds.start) {
          return bounds.start;
        }
        if (seconds > bounds.end) {
          return bounds.end;
        }
        return seconds;
      }

      function secondsToPixels(object, seconds) {
        var start = toNumber(object.start, 0);
        return (seconds - start) * scope.pixelsPerSecond;
      }

      function pushKeyframeChange(copy, ignoreRefresh) {
        var json = JSON.stringify(copy);
        if (objType === "clip") {
          timeline.update_clip_data(
            json, false /*allow keyframes*/, true /*force JSON diff*/, ignoreRefresh, transactionId
          );
        } else {
          timeline.update_transition_data(
            json, false, ignoreRefresh, transactionId
          );
        }
      }

      // Prevent parent selectable/drag handlers from interfering
      element.on("mousedown", function (e) {
        e.stopPropagation();
      });

      element.draggable({
        axis: "x",
        distance: 1,
        scroll: true,
        cursor: "ew-resize",
        start: function () {
          scope.setDragging(true);
          transactionId = uuidv4();
          currentFrame = parseInt(attrs.point, 10);
          locateObject();
          if (scope.Qt) {
            timeline.StartKeyframeDrag(objType, objId, transactionId);
          }
        },
        drag: function (e, ui) {
          locateObject();
          if (!obj || typeof obj.start === "undefined") {return;}

          var left    = ui.position.left;
          var start   = toNumber(obj.start, 0);
          var secs    = snapToFPSGridTime(scope, pixelToTime(scope, left) + start);
          var clamped = clampSeconds(obj, secs);
          if (clamped !== secs) {
            secs = clamped;
            left = secondsToPixels(obj, secs);
            ui.position.left = left;
            if (ui.helper) {
              ui.helper.css("left", left + "px");
            }
          }
          var newFrame= Math.round(secs * fps) + 1;

          if (newFrame !== currentFrame) {
            // work on a copy
            var copy = angular.copy(obj);
            scope.moveKeyframes(copy, currentFrame, newFrame);
            pushKeyframeChange(copy, true);
            currentFrame = newFrame;
          }

          // Preview frame while dragging
          var position = toNumber(obj.position, 0);
          scope.previewFrame(position + pixelToTime(scope, left));
        },
        stop: function (e, ui) {
          scope.setDragging(false);
          locateObject();
          if (!obj || typeof obj.start === "undefined") {return;}

          var left    = ui.position.left;
          var start   = toNumber(obj.start, 0);
          var secs    = snapToFPSGridTime(scope, pixelToTime(scope, left) + start);
          var clamped = clampSeconds(obj, secs);
          if (clamped !== secs) {
            secs = clamped;
            left = secondsToPixels(obj, secs);
            ui.position.left = left;
            ui.helper && ui.helper.css("left", left + "px");
          }
          var newFrame= Math.round(secs * fps) + 1;

          if (newFrame !== currentFrame) {
            var copy = angular.copy(obj);
            scope.moveKeyframes(copy, currentFrame, newFrame);
            pushKeyframeChange(copy, false);
            currentFrame = newFrame;
          }

          if (scope.Qt) {
            timeline.FinalizeKeyframeDrag(objType, objId);
          }
        }
      });
    }
  };
});
