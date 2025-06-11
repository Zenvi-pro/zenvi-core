/**
 * @file
 * @brief Keyframe directive (draggable keyframes on the timeline)
 */

/*global App, findElement, uuidv4, snapToFPSGridTime, pixelToTime, timeline*/
App.directive("tlKeyframe", function () {
  return {
    link: function (scope, element, attrs) {
      var obj = null;
      var objType = attrs.objectType;
      // Object ids are alphanumeric, so don't coerce them to numbers
      var objId = attrs.objectId;
      var fps = scope.project.fps.num / scope.project.fps.den;
      var transactionId = null;
      var currentFrame = parseInt(attrs.point, 10);

      function locateObject() {
        if (objType === "clip") {
          obj = findElement(scope.project.clips, "id", objId);
        } else if (objType === "transition") {
          obj = findElement(scope.project.effects, "id", objId);
        }
      }
      locateObject();

      function updateBackend(ignoreRefresh) {
        if (!scope.Qt || !obj) return;
        if (objType === "clip") {
          // Pass false so non-basic properties such as keyframes are updated
          timeline.update_clip_data(JSON.stringify(obj), false, true, ignoreRefresh, transactionId);
        } else if (objType === "transition") {
          // Pass false so keyframes move correctly on transitions
          timeline.update_transition_data(JSON.stringify(obj), false, ignoreRefresh, transactionId);
        }
      }

      // Prevent parent selectable/drag handlers from interfering
      element.on("mousedown", function(e) {
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
          if (!obj || typeof obj.start === "undefined") return;

          var newLeft = ui.position.left;
          var seconds = snapToFPSGridTime(scope, pixelToTime(scope, newLeft) + obj.start);
          var newFrame = Math.round(seconds * fps) + 1;

          if (newFrame !== currentFrame) {
            scope.moveKeyframes(obj, currentFrame, newFrame);
            currentFrame = newFrame;
            updateBackend(true);
          }

          // Preview frame while dragging
          var previewSeconds = obj.position + pixelToTime(scope, newLeft);
          scope.previewFrame(previewSeconds);
        },
        stop: function (e, ui) {
          scope.setDragging(false);
          locateObject();
          if (!obj || typeof obj.start === "undefined") return;

          var newLeft = ui.position.left;
          var seconds = snapToFPSGridTime(scope, pixelToTime(scope, newLeft) + obj.start);
          var newFrame = Math.round(seconds * fps) + 1;

          if (newFrame !== currentFrame) {
            scope.moveKeyframes(obj, currentFrame, newFrame);
            currentFrame = newFrame;
          }

          attrs.point = currentFrame;
          scope.$apply(function () {});
          updateBackend(false);
          if (scope.Qt) {
            timeline.FinalizeKeyframeDrag(objType, objId);
          }
        }
      });
    }
  };
});
