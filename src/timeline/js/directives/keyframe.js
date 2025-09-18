/**
 * @file
 * @brief Keyframe directive (draggable keyframes on the timeline)
 */

/*global App, findElement, uuidv4, snapToFPSGridTime, pixelToTime, timeline, angular, document*/
App.directive("tlKeyframe", function () {
  return {
    link: function (scope, element, attrs) {
      var obj, objType = attrs.objectType, objId = attrs.objectId;
      var fps = scope.project.fps.num / scope.project.fps.den;
      var transactionId = null;
      var currentFrame = parseInt(attrs.point, 10);

      // Track the candidate new frame while dragging (no model writes until stop)
      var pendingFrame = currentFrame;

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
        if (seconds < bounds.start) return bounds.start;
        if (seconds > bounds.end)   return bounds.end;
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

      var draggingKeyframe = false;

      function enterDragMode() {
        if (draggingKeyframe) { return; }
        draggingKeyframe = true;
        element.addClass("point-dragging");
        if (document && document.body) {
          document.body.classList.add("keyframe-dragging");
        }
      }

      function exitDragMode() {
        if (!draggingKeyframe) { return; }
        draggingKeyframe = false;
        element.removeClass("point-dragging");
        if (document && document.body) {
          document.body.classList.remove("keyframe-dragging");
          document.body.style.cursor = "";
        }
      }

      function restoreUserSelect() {
        if (document && document.body) {
          document.body.style.userSelect = "";
          document.body.style.webkitUserSelect = "";
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
        // keep the original behavior that worked in WebKit
        start: function () {
          scope.setDragging(true);
          enterDragMode();
          transactionId = uuidv4();
          currentFrame = parseInt(attrs.point, 10);
          pendingFrame = currentFrame;
          locateObject();
          if (scope.Qt) {
            timeline.StartKeyframeDrag(objType, objId, transactionId);
          }
          // Avoid text selection while dragging
          try { window.getSelection() && window.getSelection().removeAllRanges(); } catch (_) {}
          if (document && document.body) {
            document.body.style.userSelect = "none";
            document.body.style.webkitUserSelect = "none";
          }
        },
        drag: function (e, ui) {
          locateObject();
          if (!obj || typeof obj.start === "undefined") { return; }

          // Compute proposed position from ui.left (WebKit-friendly) and keep it clamped/snapped
          var left    = ui.position.left;
          var start   = toNumber(obj.start, 0);
          var secs    = snapToFPSGridTime(scope, pixelToTime(scope, left) + start);
          var clamped = clampSeconds(obj, secs);

          if (clamped !== secs) {
            secs = clamped;
            left = secondsToPixels(obj, secs);
            ui.position.left = left;
            if (ui.helper) ui.helper.css("left", left + "px");
          }

          // Calculate the candidate frame, but DO NOT mutate the model here
          var newFrame = Math.round(secs * fps) + 1;
          pendingFrame = newFrame;

          // Visual preview only (safe)
          var position = toNumber(obj.position, 0);
          // Use $evalAsync so Angular digest is coalesced and less likely to re-render mid-drag
          scope.$evalAsync(function () {
            scope.previewFrame(position + pixelToTime(scope, left));
          });

          // Important: no scope.moveKeyframes(...) and no pushKeyframeChange(...) here
          // Mutating here can re-render the timeline and destroy the draggable element.
        },
        stop: function (e, ui) {
          scope.setDragging(false);
          exitDragMode();
          locateObject();
          if (!obj || typeof obj.start === "undefined") {
            restoreUserSelect();
            return;
          }

          // Recompute final sec/left from the last ui.position, then COMMIT once
          var left    = ui.position.left;
          var start   = toNumber(obj.start, 0);
          var secs    = snapToFPSGridTime(scope, pixelToTime(scope, left) + start);
          var clamped = clampSeconds(obj, secs);
          if (clamped !== secs) {
            secs = clamped;
            left = secondsToPixels(obj, secs);
            ui.position.left = left;
            if (ui.helper) ui.helper.css("left", left + "px");
            element.css("left", left + "px");
          }

          var newFrame = Math.round(secs * fps) + 1;

          if (newFrame !== currentFrame) {
            // work on a copy and commit once
            var copy = angular.copy(obj);
            scope.moveKeyframes(copy, currentFrame, newFrame);
            // final commit; allow refresh here
            pushKeyframeChange(copy, false);
            currentFrame = newFrame;
          }

          if (scope.Qt) {
            timeline.FinalizeKeyframeDrag(objType, objId);
          }

          restoreUserSelect();
        }
      });

      scope.$on("$destroy", function () {
        exitDragMode();
        restoreUserSelect();
      });
    }
  };
});
