#!python3
"""OpenSim 4 to MuJoCo XML converter.

O2MConverter
Copyright 2020-2022 Aleksi Ikkala
Modified Anton Sobinov
https://github.com/aikkala/O2MConverter
"""
import os
import sys
import math
import copy
import warnings
from operator import itemgetter
from collections import OrderedDict
from shutil import copyfile

import vtk
from pyquaternion import Quaternion
from natsort import natsorted, ns
import xmltodict
import numpy as np
from scipy.interpolate import interp1d
from sklearn.metrics import r2_score
# import admesh

from . import Utils


# less than this is zero
EPSILON = 10 * sys.float_info.epsilon


class Converter4:
    """A class to convert OpenSim 4.0 XML model files to MuJoCo XML model files"""
    def __init__(self):
        # Define input XML and output folder
        self.input_xml = None
        self.output_folder = None

        self.reset()
        self.reset_mujoco_defaults()

        # Use mesh files if they are given
        self.geometry_folder = None
        self.output_geometry_folder = "Geometry/"
        self.vtk_reader = vtk.vtkXMLPolyDataReader()
        self.stl_writer = vtk.vtkSTLWriter()

        # this can be used to replace meshes in OpenSim model
        self.mesh_dic = {}

        # Setup writer
        self.stl_writer.SetInputConnection(self.vtk_reader.GetOutputPort())
        self.stl_writer.SetFileTypeToBinary()

    def reset(self):
        # List of constraints
        self.constraints = None

        # Parse bodies, joints, muscles
        self.bodies = dict()
        self.joints = dict()
        self.muscles = []

        # We need to keep track of coordinates in joints' CoordinateSet, we might need to use them
        # for setting up equality constraints
        self.coordinates = dict()

        # These dictionaries (or list of dicts) are in MuJoCo style (when converted to XML)
        self.asset = dict()
        self.tendon = []
        self.actuator = {"motor": [], "muscle": []}
        self.equality = {"joint": [], "weld": []}

        # The root of the kinematic tree
        self.origin_body = None
        self.origin_joint = None

    def reset_mujoco_defaults(self):
        # Set MuJoCo model defaults
        # Note: balanceinertia is set to true, and boundmass and boundinertia are > 0 to ignore
        # poorly designed models (that contain incorrect inertial properties or massless moving
        # bodies)
        self.mujoco_dic = {}
        self.mujoco_dic["compiler"] = {
            "@inertiafromgeom": "auto",
            "@angle": "radian",
            "@balanceinertia": "true",
            "@boundmass": "0.001",
            "@boundinertia": "0.001",
            "lengthrange": {"@inttotal": "500"}
        }
        self.mujoco_dic["default"] = {
            "joint": {
                "@limited": "true",
                "@damping": "0.5",
                "@armature": "0.01",
                "@stiffness": "0"},
            "geom": {
                "@contype": "1", "@conaffinity": "1", "@condim": "3",
                "@rgba": "0.8 0.6 .4 1",
                "@margin": "0.001",
                "@solref": ".02 1", "@solimp": ".8 .8 .01",
                "@material": "geom"},
            "site": {"@size": "0.001"},
            "tendon": {"@width": "0.001", "@rgba": ".95 .3 .3 1", "@limited": "false"},
            "default": [
                {"@class": "muscle",
                 "muscle": {"@ctrllimited": "true", "@ctrlrange": "0 1", "@scale": "400"}},
                {"@class": "motor",
                 "motor": {"@gear": "20"}}
            ]
        }
        self.mujoco_dic["option"] = {"@timestep": "0.002", "flag": {"@energy": "enable"}}
        self.mujoco_dic["size"] = {"@njmax": "1000", "@nconmax": "400", "@nuser_jnt": 1}
        self.mujoco_dic["visual"] = {
            "map": {"@fogstart": "3", "@fogend": "5", "@force": "0.1"},
            "quality": {"@shadowsize": "2048"}
        }

        # Start building the worldbody
        self.mujoco_dic["worldbody"] = {
            "geom": {
                "@name": "floor",
                "@type": "plane",
                "@pos": "0 0 0", "@size": "10 10 0.125",
                "@material": "MatPlane",
                "@condim": "3"
            },
            'body': {
                "light": {
                    "@mode": "trackcom", "@directional": "false", "@diffuse": ".8 .8 .8",
                    "@specular": "0.3 0.3 0.3", "@pos": "0 0 4.0", "@dir": "0 0 -1"
                }
            }
        }

        # Set some asset defaults
        self.mujoco_dic["asset"] = {}
        self.mujoco_dic["asset"]["texture"] = [
            {"@name": "texplane", "@type": "2d", "@builtin": "checker", "@rgb1": ".2 .3 .4",
             "@rgb2": ".1 0.15 0.2", "@width": "100", "@height": "100"},
            {"@name": "texgeom", "@type": "cube", "@builtin": "flat", "@mark": "cross",
             "@width": "127", "@height": "1278", "@rgb1": "0.8 0.6 0.4", "@rgb2": "0.8 0.6 0.4",
             "@markrgb": "1 1 1", "@random": "0.01"}
        ]
        self.mujoco_dic["asset"]["material"] = [
            {"@name": "MatPlane", "@reflectance": "0.5", "@texture": "texplane",
             "@texrepeat": "1 1", "@texuniform": "true"},
            {"@name": "geom", "@texture": "texgeom", "@texuniform": "true"}
        ]


    def convert(self, input_xml, output_folder, geometry_folder=None, for_testing=False):
        """Convert given OpenSim XML model to MuJoCo XML model

        Differently from O2MConverter, does not create a nested directory and does not extend the
        model name.
        """
        if for_testing:
            warnings.warn('for_testing flag has not been tested for OpenSim4 models.')

        # Reset all variables
        self.reset()

        # Save input and output XML files in case we need them somewhere
        self.input_xml = input_xml

        # Set geometry folder
        self.geometry_folder = geometry_folder

        # Read input_xml and parse it
        with open(input_xml) as f:
            text = f.read()
        p = xmltodict.parse(text)

        # Set output folder  AS: different from O2MConverter
        model_name = os.path.split(input_xml)[1][:-5]
        self.output_folder = output_folder

        # Create the output folder
        os.makedirs(self.output_folder, exist_ok=True)

        # Find and parse constraints
        if ("ConstraintSet" in p["OpenSimDocument"]["Model"] and
                p["OpenSimDocument"]["Model"]["ConstraintSet"]["objects"] is not None):
            self.parse_constraints(p["OpenSimDocument"]["Model"]["ConstraintSet"]["objects"])

        # TODO probably make a ground body
        # Find and parse bodies
        if "BodySet" in p["OpenSimDocument"]["Model"]:
            self.parse_bodies(p["OpenSimDocument"]["Model"]["BodySet"]["objects"])

        # Find and parse joints
        if "JointSet" in p["OpenSimDocument"]["Model"]:
            self.parse_joints(p["OpenSimDocument"]["Model"]["JointSet"]["objects"])

        # Find and parse muscles, and CoordinateLimitForces
        if ("ForceSet" in p["OpenSimDocument"]["Model"] and
                p["OpenSimDocument"]["Model"]["ForceSet"]["objects"] is not None):
            self.parse_muscles_and_tendons(p["OpenSimDocument"]["Model"]["ForceSet"]["objects"])
            if "CoordinateLimitForce" in p["OpenSimDocument"]["Model"]["ForceSet"]["objects"]:
                self.parse_coordinate_limit_forces(
                    p["OpenSimDocument"]["Model"]["ForceSet"]["objects"]["CoordinateLimitForce"])

        # If we're building this model for testing we need to unclamp all joints
        if for_testing:
            self.unclamp_all_mujoco_joints()

        # Now we need to re-assemble all of the above in MuJoCo format
        # (or actually a dict version of the model so we can use xmltodict to save the model into a
        # XML file)
        mujoco_model = self.build_mujoco_model(p["OpenSimDocument"]["Model"]["@name"])

        # If we're building this model for testing we need to disable collisions, add a camera for
        # recording, and remove the floor
        if for_testing:
            mujoco_model["mujoco"]["worldbody"]["camera"] = {
                "@name": "for_testing",
                "@pos": "0 0 0",
                "@euler": "0 0 0"}
            mujoco_model["mujoco"]["option"]["@collision"] = "predefined"
            del mujoco_model["mujoco"]["worldbody"]["geom"]

        # Finally, save the MuJoCo model into XML file
        print('Saving MuJoCo model to file...')
        output_xml = os.path.join(self.output_folder, model_name + ".xml")
        with open(output_xml, 'w') as f:
            f.write(xmltodict.unparse(mujoco_model, pretty=True, indent="  "))
        print('Done.')

        # We might need to fix stl files (if converted from OpenSim Geometry vtk files)
        if self.geometry_folder is not None:
            try:
                import admesh
                self.fix_stl_files()
            except ModuleNotFoundError as e:
                warnings.warn('Could not import admesh. Not fixing STL files.')

    def parse_constraints(self, p):
        print('Parsing constraints...')

        # Go through all (possibly different kinds of) constraints
        for constraint_type in p:
            # Make sure we're dealing with a list
            if isinstance(p[constraint_type], dict):
                p[constraint_type] = [p[constraint_type]]

            # Go through all constraints
            for constraint in p[constraint_type]:
                if ("SimmSpline" in constraint["coupled_coordinates_function"] or
                        "NaturalCubicSpline" in constraint["coupled_coordinates_function"]):
                    print('\t', constraint['@name'])

                    if "SimmSpline" in constraint["coupled_coordinates_function"]:
                        spline_type = "SimmSpline"
                    else:
                        spline_type = "NaturalCubicSpline"

                    # Get x and y values that define the spline
                    x_values = constraint["coupled_coordinates_function"][spline_type]["x"]
                    y_values = constraint["coupled_coordinates_function"][spline_type]["y"]

                    # Convert into numpy arrays
                    x_values = np.array(x_values.split(), dtype=float)
                    y_values = np.array(y_values.split(), dtype=float)

                    if len(x_values) <= 1 or len(y_values) <= 1:
                        raise ValueError("Not enough points, can't fit a spline")

                    # TODO here and in Joint: make a generalized fit function

                    # Fit a linear / quadratic / cubic / quartic function
                    fit = np.polynomial.polynomial.Polynomial.fit(
                        x_values, y_values, min(4, len(x_values)-1))

                    # A simple check to see if the fit is alright
                    y_fit = fit(x_values)
                    if r2_score(y_values, y_fit) <= 0.5:
                        raise ValueError("A bad approximation of the SimmSpline")

                    # Get the polynomial function's weights
                    polycoef = np.zeros((5,))
                    polycoef[:fit.coef.shape[0]] = fit.convert().coef

                elif "LinearFunction" in constraint["coupled_coordinates_function"]:
                    # Get coefficients of the linear function
                    coefs = np.array(constraint["coupled_coordinates_function"]["LinearFunction"][
                                     "coefficients"].split(), dtype=float)

                    # Make a quartic representation of the linear function
                    polycoef = np.zeros((5,))
                    polycoef[0] = coefs[1]
                    polycoef[1] = coefs[0]

                else:
                    raise NotImplementedError

                # Create a constraint
                # joint1 depends on joint2
                self.equality["joint"].append({
                    "@name": constraint["@name"],
                    "@joint1": constraint["dependent_coordinate_name"],
                    "@joint2": constraint["independent_coordinate_names"],
                    "@active": "true" if constraint["isEnforced"] == "true" else "false",
                    "@polycoef": Utils.array_to_string(polycoef, abs_thr=EPSILON),
                    "@solimp": "0.9999 0.9999 0.001 0.5 2"})
        print('Done.')

    def parse_bodies(self, bodyset_objects):
        print('Parsing bodies...')

        # OpenSim 4 does not have an explicit ground body
        b = Body({
            "@name": 'ground',
            "mass": '0',
            "mass_center": '0 0 0',
            'inertia': '1 1 1 0 0 0'
        })
        self.bodies[b.name] = b

        # Go through all bodies
        for obj in bodyset_objects["Body"]:
            print('\t', obj['@name'])
            b = Body(obj)

            # Add b to bodies
            self.bodies[b.name] = b
        print('Done.')

    def parse_joints(self, jointset_objects):
        print('Parsing joints...')

        # Go through all joints
        for joint_type, joint_objs in jointset_objects.items():
            print('\t', joint_type)
            for obj in joint_objs:
                print('\t\t', obj['@name'])

                j = Joint(obj, joint_type, self.equality)

                # Get coordinates, we might need them for setting up equality constraints
                self.coordinates = {**self.coordinates, **j.get_coordinates()}

                # Ignore joint if it is None
                if j.parent_body is None:
                    continue

                # Add joint equality constraints
                self.equality["joint"].extend(j.get_equality_constraints("joint"))
                self.equality["weld"].extend(j.get_equality_constraints("weld"))

                # There might be multiple joints per body
                if j.parent_body not in self.joints:
                    self.joints[j.parent_body] = []
                self.joints[j.parent_body].append(j)
        print('Done.')

    def parse_muscles_and_tendons(self, p):
        print('Parsing muscles and tendons...')

        # Go through all muscle types (typically there are only one type of muscle)
        for muscle_type in p:
            # Skip some forces
            if muscle_type == "CoordinateLimitForce":
                # We'll handle these later
                continue
            elif muscle_type not in \
                    ["Millard2012EquilibriumMuscle", "Thelen2003Muscle",
                     "Schutte1993Muscle_Deprecated", "CoordinateActuator"]:
                print("Skipping a force: {}".format(muscle_type))
                continue

            # Make sure we're dealing with a list
            if isinstance(p[muscle_type], dict):
                p[muscle_type] = [p[muscle_type]]

            # Go through all muscles
            for muscle in p[muscle_type]:
                m = Muscle(muscle, muscle_type)
                self.muscles.append(m)

                # Check if the muscle is disabled
                if m.is_disabled():
                    continue
                elif m.is_muscle:
                    self.actuator["muscle"].append(m.get_actuator())
                    self.tendon.append(m.get_tendon())

                    # Add sites to all bodies this muscle/tendon spans
                    for body_name in m.path_point_set:
                        self.bodies[body_name].add_sites(m.path_point_set[body_name])
                else:
                    self.actuator["motor"].append(m.get_actuator())
        print('Done.')

    def parse_coordinate_limit_forces(self, forces):
        print('Parsing coordinate limit forces...')
        # These parameters might be incorrect, but we'll optimize them later

        # Go through each force and set corresponding joint parameters
        for force in forces:

            # Ignore disabled forces
            if force["isDisabled"].lower() == "true":
                continue

            # Get joint name
            joint_name = force["coordinate"]

            # We need to search for this joint
            target = None
            for body in self.joints:
                for joint in self.joints[body]:
                    for mujoco_joint in joint.mujoco_joints:
                        if mujoco_joint["name"] == joint_name:
                            target = mujoco_joint

            # Check if joint was found
            assert target is not None, "Cannot set CoordinateLimitForce params, couldn't find the joint"

            # TODO for now let's ignore these forces -- they are too difficult to implement and optimize
            # Let's just switch the joint limit on if it's defined; mark this so it won't be unclamped later
            if "range" in target and target["range"][0] != target["range"][1]:
                target["limited"] = True
                target["user"] = 1
            continue

            # Take the average of stiffness
            stiffness = 0.5*(float(force["upper_stiffness"]) + float(force["lower_stiffness"]))

            # Stiffness / damping may be defined in two separate forces; we assume that we're dealing with damping
            # if average stiffness is close to zero
            if stiffness < 1e-4:

                # Check if rotational stiffness
                damping = float(force["damping"])
                if target["motion_type"] == "rotational":
                    damping *= math.pi/180

                # Set damping
                target["damping"] = damping

            else:

                # We need to create a soft joint coordinate limit, but we can't use separate limits like in OpenSim;
                # this is something we'll need to approximate

                # Limits in CoordinateLimitForce should be in degrees
                force_coordinate_limits = np.array([float(force["lower_limit"]), float(force["upper_limit"])]) * math.pi/180

                # Check if there are hard limits defined for this joint
                if target["limited"]:

                    # Range should be given if joint is limited; use range to calculate width param of solimp
                    range = target.get("range")
                    width = np.array([force_coordinate_limits[0] - range[0], range[1] - force_coordinate_limits[1]])

                    # If either width is > 0 create a soft limit
                    pos_idx = width > 0
                    if np.any(pos_idx):

                        # Mark this joint for optimization
                        target["user"] = 1

                        # Define the soft limit
                        target["solimplimit"] = [0.0001, 0.99, np.mean(width[pos_idx])]

                else:

                    # Use force_coordinate_limits as range

                    # Calculate width with the original range if it was defined
                    width = 0.001
                    if "range" in target:
                        width_range = np.array([force_coordinate_limits[0] - target["range"][0],
                                          target["range"][1] - force_coordinate_limits[1]])
                        pos_idx = width_range > 0
                        if np.any(pos_idx):
                            width = np.mean(width_range[pos_idx])

                    # Mark this joint for optimization
                    target["user"] = 1

                    # Define the soft limit
                    target["limited"] = True
                    target["solimplimit"] = [0.0001, 0.99, width, 0.5, 1]

    def build_mujoco_model(self, model_name):
        print('Building MuJoCo model...')

        # Initialize model
        model = {
            "mujoco": copy.deepcopy(self.mujoco_dic),
        }
        model["mujoco"]["@model"] = model_name

        # We should probably find the "origin" body, where the kinematic chain begins
        self.origin_body, self.origin_joint = self.find_origin()

        # Rotate self.origin_joint.orientation_in_parent so the model is upright
        # Rotation is done along an axis that goes through (0,0,0) coordinate
        T_origin_joint = Utils.create_transformation_matrix(
            self.origin_joint.location_in_parent,
            quat=self.origin_joint.orientation_in_parent)
        T_rotation = Utils.create_rotation_matrix(axis=[1, 0, 0], rad=math.pi/2)
        self.origin_joint.set_transformation_matrix(np.matmul(T_rotation, T_origin_joint))

        # Add sites to worldbody / "ground" in OpenSim
        model["mujoco"]["worldbody"]["site"] = self.bodies[self.origin_joint.parent_body].sites

        # Build the kinematic chains
        model["mujoco"]["worldbody"]["body"] = self.add_body(
            model["mujoco"]["worldbody"]["body"], self.origin_body,
            self.joints[self.origin_body.name])

        # We might want to use a weld constraint to fix the origin body to worldbody for experiments
        self.equality["weld"].append({
            "@name": "origin_to_worldbody",
            "@body1": self.origin_body.name,
            "@active": "false"})

        # Add assets to model
        model["mujoco"]["asset"].update(self.asset)

        # Add tendons and actuators
        model["mujoco"]["tendon"] = {"spatial": self.tendon}
        model["mujoco"]["actuator"] = self.actuator

        # Add equality constraints between joints; note that we may need to remove some equality
        # constraints that were set in ConstraintSet but then overwritten or not used
        remove_idxs = []
        for idx, constraint in enumerate(self.equality["joint"]):
            constraint_found = False
            for joints in self.joints.values():
                for joint in joints:
                    for mujoco_joint in joint.mujoco_joints:
                        if mujoco_joint["name"] == constraint["@joint1"]:
                            constraint_found = True

            if not constraint_found:
                remove_idxs.append(idx)
                # self.equality["joint"].remove(constraint)

        # Remove constraints that aren't used
        for idx in sorted(remove_idxs, reverse=True):
            del self.equality["joint"][idx]

        # Add equality constraints into the model
        model["mujoco"]["equality"] = self.equality

        print('Done.')

        return model

    def unclamp_all_mujoco_joints(self):
        # Unclamp (set limited=false) all joints except those that have limits that need to be
        # optimized
        for joints in self.joints.values():
            for j in self.joints:
                for mujoco_joint in j.mujoco_joints:
                    # Don't unclamp dependent joints or joints that have limits that need to be
                    # optimized
                    if (mujoco_joint["motion_type"] not in ["dependent", "coupled"] and
                            not ("user" in mujoco_joint and mujoco_joint["user"] == 1)):
                        mujoco_joint["limited"] = False

    def add_body(self, worldbody, current_body, current_joints):
        # Create a new MuJoCo body
        worldbody["@name"] = current_body.name

        # We need to find this body's position relative to parent body:
        # since we're progressing down the kinematic chain, each body
        # should have a joint to parent body
        joint_to_parent = self.find_joint_to_parent(current_body.name)

        # Update location and orientation of child body
        T = Utils.create_transformation_matrix(joint_to_parent.location,
                                               quat=joint_to_parent.orientation)
        joint_to_parent.set_transformation_matrix(
            np.matmul(joint_to_parent.get_transformation_matrix(), np.linalg.inv(T)))

        # Define position and orientation
        worldbody["@pos"] = Utils.array_to_string(joint_to_parent.location_in_parent,
                                                  abs_thr=EPSILON)
        worldbody["@quat"] = Utils.array_to_string([
            joint_to_parent.orientation_in_parent.w,
            joint_to_parent.orientation_in_parent.x,
            joint_to_parent.orientation_in_parent.y,
            joint_to_parent.orientation_in_parent.z], abs_thr=EPSILON)

        # Add geom
        worldbody["geom"] = self.add_geom(current_body)

        # Add inertial properties -- only if mass is greater than zero and eigenvalues are positive
        # (if "inertial" is missing MuJoCo will infer the inertial properties from geom)
        if current_body.mass > 0:
            values, vectors = np.linalg.eig(Utils.create_symmetric_matrix(current_body.inertia))
            if np.all(values > 0):
                worldbody["inertial"] = {
                    "@pos": Utils.array_to_string(current_body.mass_center),
                    "@mass": str(current_body.mass),
                    "@fullinertia": Utils.array_to_string(current_body.inertia)}
            elif np.all(np.array(current_body.inertia) == 0):
                # ARS: if the inertia was unset, ignore it, only save mass and mass center
                worldbody["inertial"] = {
                    "@pos": Utils.array_to_string(current_body.mass_center),
                    "@mass": str(current_body.mass)}

        # Add sites
        worldbody["site"] = current_body.sites

        # Go through joints
        worldbody["joint"] = []
        for mujoco_joint in joint_to_parent.mujoco_joints:
            # Define the joint
            j = {"@name": mujoco_joint["name"],
                 "@type": mujoco_joint["type"],
                 "@pos": "0 0 0",
                 "@axis": Utils.array_to_string(mujoco_joint["axis"])}
            if "limited" in mujoco_joint:
                j["@limited"] = "true" if mujoco_joint["limited"] else "false"
            if "range" in mujoco_joint:
                j["@range"] = Utils.array_to_string(mujoco_joint["range"])
            if "ref" in mujoco_joint:
                j["@ref"] = str(mujoco_joint["ref"])
            if "springref" in mujoco_joint:
                j["@springref"] = str(mujoco_joint["springref"])
            if "stiffness" in mujoco_joint:
                j["@stiffness"] = str(mujoco_joint["stiffness"])
            if "damping" in mujoco_joint:
                j["@damping"] = str(mujoco_joint["damping"])
            if "solimplimit" in mujoco_joint:
                j["@solimplimit"] = Utils.array_to_string(mujoco_joint["solimplimit"])
            if "user" in mujoco_joint:
                j["@user"] = str(mujoco_joint["user"])

            # If the joint is between origin body and it's parent, which should be "ground", set
            # damping, armature, and stiffness to zero
            if joint_to_parent is self.origin_joint:
                j.update({"@armature": 0, "@damping": 0, "@stiffness": 0})

            # Add to joints
            worldbody["joint"].append(j)

        # And we're done if there are no joints
        if current_joints is None:
            return worldbody

        worldbody["body"] = []
        for j in current_joints:
            worldbody["body"].append(self.add_body(
                {}, self.bodies[j.child_body],
                self.joints.get(j.child_body, None)
            ))

        return worldbody

    def add_geom(self, body):
        # Collect all geoms here
        geom = []

        if self.geometry_folder is None:
            # By default use a capsule
            # Try to figure out capsule size by mass or something
            # FIX(AS) TODO(AS)
            # Some objects should not have a geometry, e.g. phantom objects needed for complex
            # transformations. Sizes cannot be defined as zero
            if body.mass > 0:
                size = np.array([0.01, 0.01])*np.sqrt(body.mass)
                geom.append({"@name": body.name, "@type": "capsule",
                             "@size": Utils.array_to_string(size)})

        else:
            # Make sure output geometry folder exists
            os.makedirs(os.path.join(self.output_folder, self.output_geometry_folder),
                        exist_ok=True)

            # Grab the mesh from given geometry folder
            for m in body.mesh:
                # Get file path
                geom_file = os.path.join(self.geometry_folder, m["mesh_file"])

                # Check the file exists
                if not os.path.exists(geom_file) or not os.path.isfile(geom_file):
                    if geom_file[-3:] == "vtp":
                        if (os.path.exists(geom_file[:-3] + 'stl') and
                                os.path.isfile(geom_file[:-3] + 'stl')):
                            warnings.warn('Could not find a VTP file, using found STL file.')
                            geom_file = geom_file[:-3] + 'stl'
                        else:
                            raise ValueError("Neither STL or VTP files {} exist".format(geom_file))
                    else:
                        raise ValueError("Mesh file {} doesn't exist".format(geom_file))

                # Transform vtk into stl or just copy stl file
                mesh_name = m["mesh_file"][:-4]
                stl_file = os.path.join(self.output_geometry_folder, mesh_name + ".stl")

                # Add mesh to asset - and if it is not a duplicate, transform or copy it
                if self.add_mesh_to_asset(mesh_name, stl_file, m):
                    # Transform a vtk file into an stl file and save it
                    if geom_file[-3:] == "vtp":
                        self.vtk_reader.SetFileName(geom_file)
                        self.stl_writer.SetFileName(os.path.join(self.output_folder, stl_file))
                        self.stl_writer.Write()

                    # Just copy stl file
                    elif geom_file[-3:] == "stl":
                        copyfile(geom_file, os.path.join(self.output_folder, stl_file))

                    else:
                        raise NotImplementedError("Geom file is not vtk or stl!")

                # Create the geom
                # one mesh can be used for multiple geoms, but all geoms have to have different
                # names
                geom.append({"@name": '{}_{}'.format(body.name, mesh_name), "@type": "mesh",
                            "@mesh": mesh_name})

        return geom

    def add_mesh_to_asset(self, mesh_name, mesh_file, mesh):
        if "mesh" not in self.asset:
            self.asset["mesh"] = []
        # do not duplicate meshes
        if mesh_name not in [v['@name'] for v in self.asset["mesh"]]:
            self.asset["mesh"].append({"@name": mesh_name,
                                       "@file": mesh_file,
                                       "@scale": mesh["scale_factors"]})
            return True
        # means duplicate
        return False

    def find_origin(self):
        # Start from a random joint and work your way backwards until you find
        # the origin body (the body that represents ground)

        # Make sure there's at least one joint
        if len(self.joints) == 0:
            raise ValueError("There are no joints!")

        # Choose a joint, doesn't matter which one
        current_joint = next(iter(self.joints.values()))[0]

        # Follow the kinematic chain
        new_joint_found = True
        while new_joint_found:
            # Move up in the kinematic chain as far as possible
            new_joint_found = False
            for parent_body in self.joints:
                for j in self.joints[parent_body]:
                    if j.child_body == current_joint.parent_body:
                        current_joint = j
                        new_joint_found = True
                        break

        # No further joints, child of current joint is the origin body
        return self.bodies[current_joint.child_body], current_joint

    def find_joint_to_parent(self, body_name):
        joint_to_parent = None
        for joints in self.joints.values():
            for joint in joints:
                if joint.child_body == body_name:
                    joint_to_parent = joint

            # If there are multiple child bodies with the same name, the last
            # one is returned
            if joint_to_parent is not None:
                break

        if joint_to_parent is None:
            raise ValueError("Couldn't find joint to parent body for body {}".format(body_name))

        return joint_to_parent

    def fix_stl_files(self):
        print('Transforming and fixing STL files...')
        # Loop through geometry folder and fix stl files
        for mesh_file in os.listdir(os.path.join(self.output_folder, self.output_geometry_folder)):
            if mesh_file.endswith(".stl"):
                mesh_file = os.path.join(self.output_folder, self.output_geometry_folder, mesh_file)
                stl = admesh.Stl(mesh_file)
                stl.remove_unconnected_facets()
                stl.write_binary(mesh_file)
        print('Done.')


class Joint:
    def __init__(self, joint, joint_type, constraints):
        self.reset()

        self.joint_type = joint_type

        for frame in joint['frames']['PhysicalOffsetFrame']:
            if frame['@name'] == joint['socket_parent_frame']:
                parent_frame = frame
            if frame['@name'] == joint['socket_child_frame']:
                child_frame = frame

        # Get names of bodies this joint connects
        self.parent_body = parent_frame['socket_parent']
        self.child_body = child_frame['socket_parent']
        # rm/fix "/bodyset" in names
        if self.parent_body == '/ground':
            self.parent_body = 'ground'
        if self.parent_body.startswith('/bodyset/'):
            self.parent_body = self.parent_body[9:]
        if self.child_body.startswith('/bodyset/'):
            self.child_body = self.child_body[9:]

        # And other parameters
        self.location_in_parent = np.array(parent_frame['translation'].split(), dtype=float)
        self.location = np.array(child_frame['translation'].split(), dtype=float)
        orientation = np.array(child_frame["orientation"].split(), dtype=float)
        x = Quaternion(axis=[1, 0, 0], radians=orientation[0]).rotation_matrix
        y = Quaternion(axis=[0, 1, 0], radians=orientation[1]).rotation_matrix
        z = Quaternion(axis=[0, 0, 1], radians=orientation[2]).rotation_matrix
        self.orientation = Quaternion(matrix=np.matmul(np.matmul(x, y), z))

        # Calculate orientation in parent
        orientation_in_parent = np.array(parent_frame["orientation"].split(), dtype=float)
        x = Quaternion(axis=[1, 0, 0], radians=orientation_in_parent[0]).rotation_matrix
        y = Quaternion(axis=[0, 1, 0], radians=orientation_in_parent[1]).rotation_matrix
        z = Quaternion(axis=[0, 0, 1], radians=orientation_in_parent[2]).rotation_matrix
        self.orientation_in_parent = Quaternion(matrix=np.matmul(np.matmul(x, y), z))

        # Not sure if we should update child body location and orientation before or after parsing
        # joints; at the moment we're doing it after
        # T = Utils.create_transformation_matrix(self.location, quat=self.orientation)
        # self.set_transformation_matrix(
        #    np.matmul(self.get_transformation_matrix(), np.linalg.inv(T)))

        # Some joint values are dependent on other joint values; we need to create equality
        # constraints between those
        # Also we might need to use weld constraints on locked joints
        self.equality_constraints = {"joint": [], "weld": []}

        # CustomJoint can represent any joint, we need to figure out
        # what kind of joint we're dealing with
        self.mujoco_joints = []
        if self.joint_type == "CustomJoint":
            T_joint = self.parse_custom_joint(joint, constraints)

            # Update joint location and orientation
            T = self.get_transformation_matrix()
            T = np.matmul(T, T_joint)
            self.set_transformation_matrix(T)

        elif self.joint_type == "WeldJoint":
            # TODO(AS)? need to add fixed translations and rotations?
            # Don't add anything to self.mujoco_joints, bodies are by default
            # attached rigidly to each other in MuJoCo
            pass

        else:
            raise NotImplementedError('Joint type {}'.format(self.joint_type))

    def reset(self):
        self.parent_body = None
        self.coordinates = dict()

    def get_transformation_matrix(self):
        T = self.orientation_in_parent.transformation_matrix
        T[:3, 3] = self.location_in_parent
        return T

    def set_transformation_matrix(self, T):
        self.orientation_in_parent = Quaternion(matrix=T)
        self.location_in_parent = T[:3, 3]

    def parse_custom_joint(self, joint, constraints):
        # A CustomJoint in OpenSim model can represent any type of joint.
        # Try to parse the CustomJoint into a set of MuJoCo joints

        # Get transform axes
        transform_axes = joint["SpatialTransform"]["TransformAxis"]

        # We might need to create a homogeneous transformation matrix from
        # location_in_parent to actual joint location
        T = np.eye(4, 4)
        # T = self.orientation_in_parent.transformation_matrix

        # Start by parsing the CoordinateSet
        coordinate_set = self.parse_coordinate_set(joint)

        # NOTE! Coordinates in CoordinateSet parameterize this joint. In theory all six DoFs could
        # be dependent on one Coordinate. Here we assume that only one DoF is equivalent to a
        # Coordinate, that is, there exists an identity mapping between a Coordinate and a DoF,
        # which is different to OpenSim where there might be no identity mappings. In OpenSim a
        # Coordinate is just a value and all DoFs might have some kind of mapping with it, see e.g.
        # "flexion" Coordinate in MoBL_ARMS_module6_7_CMC.osim model. MuJoCo doesn't have such
        # abstract notion of a "Coordinate", and thus there cannot be a non-identity mapping from a
        # joint to itself

        # Go through axes; there's something wrong with the order of transformations, this is the
        # order that works for leg6dof9musc.osim and MoBL_ARMS_module6_7_CMC.osim models, but it's
        # so weird it's likely to be incorrect
        transforms = ["rotation1", "rotation2", "rotation3",
                      "translation1", "translation2", "translation3"]
        order = [5, 4, 3, 0, 1, 2]
        # order = [0, 1, 2, 3, 4, 5]
        dof_designated = []
        for idx in order:
            t = transform_axes[idx]
            if t["@name"] != transforms[idx]:
                raise IndexError("Joints are parsed in incorrect order")

            # Use the Coordinate parameters we parsed earlier; note that these do not exist for all
            # joints (e.g constant joints)
            if t.get("coordinates", None) in coordinate_set:
                coord_params = copy.deepcopy(coordinate_set[t["coordinates"]])
            else:
                coord_params = {
                    "name": "{}_{}".format(joint["@name"], t["@name"]),
                    "limited": False,
                    "transform_value": 0,
                    "coordinates": "unspecified"}
            coord_params["original_name"] = coord_params["name"]
            # in OpenSim 4 the type of transformation is not specified in the coordinate parameters
            coord_params["motion_type"] = transforms[idx]

            # Set default reference position/angle to zero. If this value is not zero, then you need
            # more care while calculating quartic functions for equality constraints
            coord_params["ref"] = 0

            # By default add this joint to MuJoCo model
            coord_params["add_to_mujoco_joints"] = True

            # See the comment before this loop. We have to designate one DoF per Coordinate as an
            # independent variable, i.e. make its dependence linear
            if ("coordinates" in t and
                    t["coordinates"] == coord_params["name"] and
                    t["@name"].startswith(coord_params["motion_type"][:8]) and  # possibly redundant
                    not coord_params["name"] in dof_designated):
                # This is not necessary if the coordinate is dependent on another coordinate...
                # starting to get complicated
                # (no shit)
                ignore = False
                if "joint" in constraints:
                    for c in constraints["joint"]:
                        if coord_params["name"] == c["@joint1"]:
                            ignore = True
                            break

                if not ignore:
                    # AS: just skipping this part. I think the range is updated in the later
                    # processing of polynomials, too
                    # Also, it needs to have a condition for Constant for the O2MConverter so it
                    # does not just break?

                    # Check if we need to modify limits, TODO not sure if this is correct or needed
                    if Utils.is_nested_field(t, "SimmSpline", ["function"]):
                        warnings.warn(
                            'DOF {} is defined as a spline. In OpenSim, for input and output it'
                            ' will be using radians, as opposed to all other rotational DOFs that'
                            ' will use degrees, needlessly complicating the pipeline. Consider'
                            ' changing this DOF to a linear one, otherwise who knows what will'
                            ' happen.'.format(t["@name"]))

                        # Fit a line/spline and check limit values within that fit
                        x_values = np.array(t["function"]["SimmSpline"]["x"].split(), dtype=float)
                        y_values = np.array(t["function"]["SimmSpline"]["y"].split(), dtype=float)
                        assert len(x_values) > 1 and len(y_values) > 1, "Not enough points, can't fit a polynomial"
                        fit = np.polynomial.polynomial.Polynomial.fit(x_values, y_values, min(4, len(x_values) - 1))
                        y_fit = fit(x_values)
                        assert r2_score(y_values, y_fit) > 0.5, "A bad approximation of the SimmSpline"

                        # Update range as min/max of the approximated range
                        coord_params["range"] = np.array([min(y_fit), max(y_fit)])

                        # Make this into an identity mapping
                        t["function"] = dict({"LinearFunction": {"coefficients": '1 0'}})

                    elif Utils.is_nested_field(t, "SimmSpline", []):
                        warnings.warn(
                            'DOF {} is defined as a spline. In OpenSim, for input and output it'
                            ' will be using radians, as opposed to all other rotational DOFs that'
                            ' will use degrees, needlessly complicating the pipeline. Consider'
                            ' changing this DOF to a linear one, otherwise who knows what will'
                            ' happen.'.format(t["@name"]))

                        # Fit a line/spline and check limit values within that fit
                        x_values = np.array(t["SimmSpline"]["x"].split(), dtype=float)
                        y_values = np.array(t["SimmSpline"]["y"].split(), dtype=float)
                        assert len(x_values) > 1 and len(y_values) > 1, "Not enough points, can't fit a polynomial"
                        fit = np.polynomial.polynomial.Polynomial.fit(x_values, y_values, min(4, len(x_values) - 1))
                        y_fit = fit(x_values)
                        assert r2_score(y_values, y_fit) > 0.5, "A bad approximation of the SimmSpline"

                        # Update range as min/max of the approximated range
                        coord_params["range"] = np.array([min(y_fit), max(y_fit)])

                        # Make this into an identity mapping
                        t["function"] = dict({"LinearFunction": {"coefficients": '1 0'}})

                    elif Utils.is_nested_field(t, "LinearFunction", ["function"]):
                        coefficients = np.array(t["function"]["LinearFunction"]["coefficients"].split(), dtype=float)
                        assert abs(coefficients[0]) == 1 and coefficients[1] == 0, "Should we modify limits?"

                    elif Utils.is_nested_field(t, "LinearFunction", []):
                        coefficients = np.array(t["LinearFunction"]["coefficients"].split(), dtype=float)
                        assert abs(coefficients[0]) == 1 and coefficients[1] == 0, "Should we modify limits?"

                    elif (Utils.is_nested_field(t, "Constant", []) or
                          Utils.is_nested_field(t, "Constant", ['function', 'MultiplierFunction'])):
                        # unused DOFs on all joints
                        pass

                    else:
                        print(t)
                        # raise NotImplementedError

                    # Mark this dof as designated
                    dof_designated.append(coord_params["name"])

            elif coord_params["name"] in dof_designated:
                # A DoF has already been designated for a coordinate with coord_params["name"],
                # rename this joint
                coord_params["name"] = "{}_{}".format(coord_params["name"], t["@name"])

            # Handle a "Constant" transformation. We're not gonna create this joint
            # but we need the transformation information to properly align the joint
            flip_axis = False
            if ("Constant" in t or
                    Utils.is_nested_field(t, "Constant", ["MultiplierFunction", "function"])):

                # Get the value
                if "MultiplierFunction" in t:
                    value = float(t["MultiplierFunction"]["function"]["Constant"]["value"])
                elif "Constant" in t:
                    value = float(t["Constant"]["value"])
                else:
                    raise NotImplementedError

                # If the value is near zero don't bother creating this joint
                if abs(value) < 10 * sys.float_info.epsilon:
                    continue

                # Otherwise define a limited MuJoCo joint (we're not really creating this
                # (sub)joint, we just update the joint position)
                coord_params["limited"] = True
                coord_params["range"] = np.array([value])
                coord_params["transform_value"] = value
                coord_params["add_to_mujoco_joints"] = False

            # Handle a "SimmSpline" or "NaturalCubicSpline" transformation with a quartic
            # approximation
            elif (Utils.is_nested_field(t, "SimmSpline", ["MultiplierFunction", "function"]) or
                  Utils.is_nested_field(t, "NaturalCubicSpline",
                                        ["MultiplierFunction", "function"]) or
                  Utils.is_nested_field(t, "SimmSpline", []) or
                  Utils.is_nested_field(t, "NaturalCubicSpline", [])):

                # We can't model the relationship between two joints using a spline, but we can try
                # to approximate it with a quartic function. So fit a quartic function and check
                # that the error is small enough

                # Get spline values
                if Utils.is_nested_field(t, "SimmSpline", []):
                    x_values = t["SimmSpline"]["x"]
                    y_values = t["SimmSpline"]["y"]
                elif Utils.is_nested_field(t, "NaturalCubicSpline", []):
                    x_values = t["NaturalCubicSpline"]["x"]
                    y_values = t["NaturalCubicSpline"]["y"]
                elif Utils.is_nested_field(t, "SimmSpline", ["MultiplierFunction", "function"]):
                    x_values = t["MultiplierFunction"]["function"]["SimmSpline"]["x"]
                    y_values = t["MultiplierFunction"]["function"]["SimmSpline"]["y"]
                else:
                    x_values = t["MultiplierFunction"]["function"]["NaturalCubicSpline"]["x"]
                    y_values = t["MultiplierFunction"]["function"]["NaturalCubicSpline"]["y"]

                # Convert into numpy arrays
                x_values = np.array(x_values.split(), dtype=float)
                y_values = np.array(y_values.split(), dtype=float)

                if len(x_values) <= 1 or len(y_values) <= 1:
                    raise ValueError("Not enough points, can't fit a spline")

                # Fit a linear / quadratic / cubic / quartic function
                fit = np.polynomial.polynomial.Polynomial.fit(
                    x_values, y_values, min(4, len(x_values) - 1))

                # A simple check to see if the fit is alright
                y_fit = fit(x_values)
                if r2_score(y_values, y_fit) <= 0.5:
                    raise ValueError("A bad approximation of the SimmSpline")

                # Get the weights
                polycoef = np.zeros((5,))
                polycoef[:fit.coef.shape[0]] = fit.convert().coef

                # Update name; since this is a dependent joint variable the independent joint
                # variable might already have this name
                if coord_params["name"] == coord_params["original_name"] and False:  # FIX
                    coord_params["name"] = "{}_{}".format(coord_params["name"], t["@name"])
                coord_params["limited"] = True

                # Get min and max values
                y_fit = fit(x_values)
                coord_params["range"] = np.array([min(y_fit), max(y_fit)])

                # Add a joint constraint between this joint and the independent joint, which we
                # assume to be named t["coordinates"]
                independent_joint = t["coordinates"]

                # Some dependent joint values may be coupled to another joint values. We need to
                # find the name of the independent joint
                # TODO We could do this after the model has been built since we just swap joint
                # names, then we wouldn't need to pass constraints into body/joint parser
                # coord_params["motion_type"] is typically "coupled" for dependent joints, but not
                # always, so let's just loop through constraints and check
                # constraint_found = False

                # Go through all joint equality constraints
                for c in constraints["joint"]:
                    if c["@joint1"] == t["coordinates"]:
                        # constraint_found = True

                        # Check if this constraint is active
                        if c["@active"] != "true":
                            break

                        # Change the name of the independent joint
                        independent_joint = c["@joint2"]

                        # We're handling only an identity transformation for now
                        coeffs = np.array(c["@polycoef"].split(), dtype=float)
                        if not np.array_equal(coeffs, np.array([0, 1, 0, 0, 0])):
                            raise NotImplementedError(
                                "We're handling only identity transformations for now")

                        break

                # assert constraint_found, "Couldn't find an independent joint for a coupled joint"

                # else:
                # Update motion type to dependent for posterity
                coord_params["motion_type"] = "dependent"

                # These joint equality constraints don't seem to work properly. Is it because
                # they're soft constraints? E.g. the translations between femur and tibia should be
                # strictly defined by knee angle, but it seems like they're affected by gravity as
                # well (tibia drops to translation range limit value when leg6dof9musc is hanging
                # from air) -> seems to work when solimp limits are very tight

                coord_params["add_to_mujoco_joints"] = True

                # TODO fix by adding the original DOF, too?

                # Add the equality constraint
                if coord_params["add_to_mujoco_joints"] and False:  # FIX
                    # We don't want to create a transform so set transform_value to zero
                    coord_params["transform_value"] = 0
                    self.equality_constraints["joint"].append({
                        "@name": coord_params["name"] + "_constraint",
                        "@active": "true",
                        "@joint1": coord_params["name"],
                        "@joint2": independent_joint,
                        "@polycoef": Utils.array_to_string(polycoef, abs_thr=EPSILON),
                        "@solimp": "0.9999 0.9999 0.001 0.5 2"})

            elif "LinearFunction" in t:
                # I'm not sure how to handle a LinearFunction with coefficients != [1, 0] (the
                # first one is slope, second intercept), except for [-1, 0] when we can just flip
                # the axis
                coefficients = np.array(t["LinearFunction"]["coefficients"].split(), dtype=float)
                if abs(coefficients[0]) != 1 or coefficients[1] != 0:
                    raise NotImplementedError("How do we handle this linear function?")

                # If first coefficient is negative, flip the joint axis
                if coefficients[0] < 0:
                    flip_axis = True

                # Don't use transform_value here; we just want to use this joint as a mujoco joint
                # NOTE! We do need the transform_value for weld constraint if this joint is locked
                if "locked" in coord_params and coord_params["locked"]:
                    coord_params["default_value_for_locked"] = coord_params["transform_value"]
                coord_params["transform_value"] = 0

            # Other functions are not defined yet
            else:
                warnings.warn("Skipping transformation: {}".format(t))

            # Calculate new axis
            axis = np.array(t["axis"].split(), dtype=float)
            new_axis = np.matmul(self.orientation.transformation_matrix,
                                 Utils.create_transformation_matrix(axis))[:3, 3]
            coord_params["axis"] = new_axis
            if flip_axis:
                coord_params["axis"] *= -1

            # Figure out whether this is rotation or translation
            if t["@name"].startswith('rotation'):
                coord_params["type"] = "hinge"
            elif t["@name"].startswith('translation'):
                coord_params["type"] = "slide"
            else:
                raise TypeError("Unidentified transformation {}".format(t["@name"]))

            # If we add this joint then need to update T
            if coord_params["transform_value"] != 0:
                if coord_params["type"] == "hinge":
                    T_t = Utils.create_rotation_matrix(coord_params["axis"],
                                                       coord_params["transform_value"])
                else:
                    T_t = Utils.create_translation_matrix(coord_params["axis"],
                                                          coord_params["transform_value"])
                T = np.matmul(T, T_t)

            # Check if this joint/transformation should be added to mujoco_joints
            if coord_params["add_to_mujoco_joints"]:
                self.mujoco_joints.append(coord_params)

                # We might need this coordinate later for setting equality constraints between
                # joints
                self.coordinates[t["coordinates"]] = coord_params

            # We need to add an equality constraint for locked joints
            if "locked" in coord_params and coord_params["locked"]:
                # Create the constraint
                polycoef = np.array([coord_params["default_value_for_locked"], 0, 0, 0, 0])
                constraint = {"@name": coord_params["name"] + "_constraint",
                              "@active": "true",
                              "@joint1": coord_params["name"],
                              "@polycoef": Utils.array_to_string(polycoef)}

                # Add to equality constraints
                self.equality_constraints["joint"].append(constraint)

        return T

    @staticmethod
    def parse_coordinate_set(joint):
        # Parse all Coordinates defined for this joint
        coordinate_set = OrderedDict()
        if 'coordinates' in joint.keys() and 'Coordinate' in joint['coordinates'].keys():
            coordinates = joint["coordinates"]["Coordinate"]

            # Make sure coordinates is a list
            if isinstance(coordinates, dict):
                coordinates = [coordinates]

            # Parse all Coordinates
            for c in coordinates:
                coordinate_set[c["@name"]] = {
                    "name": c["@name"],
                    "range": np.array(c["range"].split(), dtype=float),
                    "limited": True if c["clamped"].lower() == "true" else False,
                    "locked": True if c["locked"].lower() == "true" else False,
                    "transform_value": float(c["default_value"]) if "default_value" in c else None}

        return coordinate_set

    def get_equality_constraints(self, constraint_type):
        return self.equality_constraints[constraint_type]

    def get_coordinates(self):
        return self.coordinates


class Body:
    def __init__(self, obj):
        # Initialize parameters
        self.sites = []

        # Get important attributes
        self.name = obj["@name"]
        self.mass = float(obj["mass"])
        self.mass_center = np.array(obj["mass_center"].split(), dtype=float)
        if 'inertia' in obj.keys():
            self.inertia = np.fromstring(obj['inertia'], dtype=float, sep=' ')
        else:
            self.inertia = np.array([obj[x] for x in
                                    ["inertia_xx", "inertia_yy", "inertia_zz",
                                     "inertia_xy", "inertia_xz", "inertia_yz"]], dtype=float)

        # Get meshes if there are VisibleObjects
        self.mesh = []
        if "attached_geometry" in obj and obj['attached_geometry'] is not None:
            # Get scaling of attached_geometry (might not exist in OpenSim4)
            if 'scale_factors' in obj["attached_geometry"]:
                visible_object_scale = np.array(obj["attached_geometry"]["scale_factors"].split(),
                                                dtype=float)
            else:
                visible_object_scale = np.ones((3, ))

            if "Mesh" in obj["attached_geometry"]:
                # Get mesh / list of meshes
                geometry = obj["attached_geometry"]["Mesh"]

                if isinstance(geometry, dict):
                    geometry = [geometry]

                for g in geometry:
                    display_geometry_scale = np.array(g["scale_factors"].split(), dtype=float)
                    total_scale = visible_object_scale * display_geometry_scale
                    g["scale_factors"] = Utils.array_to_string(total_scale)
                    self.mesh.append(g)

            else:
                print("No geometry files for body [{}]".format(self.name))

    def add_sites(self, path_point):
        for point in path_point:
            self.sites.append({"@name": point["@name"], "@pos": point["location"]})


class Muscle:
    def __init__(self, obj, muscle_type):
        raise DeprecationWarning('Has not verified that this works for OpenSim4.')
        # Note: Muscle class represents other types of actuators (just CoordinateActuator at the
        # moment) as well
        self.muscle_type = muscle_type
        if muscle_type in ["CoordinateActuator", "PointActuator", "TorqueActuator"]:
            self.is_muscle = False
        else:
            self.is_muscle = True

        # Get important attributes
        self.name = obj["@name"]
        self.disabled = False if "isDisabled" not in obj or obj["isDisabled"] == "false" else True

        # Parse time constants
        self.timeconst = np.ones((2, 1))
        self.timeconst.fill(np.nan)
        if "activation_time_constant" in obj:
            self.timeconst[0] = obj["activation_time_constant"]
        elif "activation1" in obj:
            self.timeconst[0] = obj["activation1"]
        if "deactivation_time_constant" in obj:
            self.timeconst[1] = obj["deactivation_time_constant"]
        elif "activation2" in obj:
            self.timeconst[1] = obj["activation2"]

        # TODO I'm not sure if this is what time_scale means, but activation/deactivation times
        # seem very large otherwise
        if "time_scale" in obj:
            time_scale = np.array(obj["time_scale"].split(), dtype=float)
            self.timeconst *= time_scale

        # TODO We're adding length ranges here because MuJoCo's automatic computation fails. Not
        # sure how they should be calculated though, these values are most likely incorrect
        # ==> this is possibly fixed, just needed to give longer simulation time for the automatic
        # computation
        self.length_range = np.array([0, 2])
        if "tendon_slack_length" in obj:
            self.tendon_slack_length = obj["tendon_slack_length"]
            # self.length_range = np.array([0.025*float(self.tendon_slack_length),
            #                               40*float(self.tendon_slack_length)])

        # Get damping for tendon -- not sure what the unit in OpenSim is, or how it relates to
        # MuJoCo damping parameter
        self.tendon_damping = obj.get("damping", None)

        # Let's use max isometric force as an approximation for muscle scale parameter in MuJoCo
        self.scale = obj.get("max_isometric_force", None)

        # Parse control limits
        self.limit = np.ones((2, 1))
        self.limit.fill(np.nan)
        if "min_control" in obj:
            self.limit[0] = obj["min_control"]
        if "max_control" in obj:
            self.limit[1] = obj["max_control"]

        # Get optimal force if defined (for non-muscle actuators only?)
        self.optimal_force = obj.get("optimal_force", None)

        # Get coordinate on which this actuator works (for non-muscle actuators only)
        self.coordinate = obj.get("coordinate", None)

        # Get path points so we can later add them into bodies; note that we treat
        # each path point type (i.e. PathPoint, ConditionalPathPoint, MovingPathPoint)
        # as a fixed path point; also note that non-muscle actuators don't have GeometryPaths
        if self.is_muscle:
            self.path_point_set = dict()
            self.sites = []
            path_point_set = obj["GeometryPath"]["PathPointSet"]["objects"]
            for pp_type in path_point_set:

                # TODO We're defining MovingPathPoints as fixed PathPoints and ignoring
                # ConditionalPathPoints

                # Put the dict into a list of it's not already
                if isinstance(path_point_set[pp_type], dict):
                    path_point_set[pp_type] = [path_point_set[pp_type]]

                # Go through all path points
                for path_point in path_point_set[pp_type]:
                    if path_point["body"] not in self.path_point_set:
                        self.path_point_set[path_point["body"]] = []

                    if pp_type == "PathPoint":
                        # A normal PathPoint, easy to define
                        self.path_point_set[path_point["body"]].append(path_point)
                        self.sites.append({"@site": path_point["@name"]})

                    elif pp_type == "ConditionalPathPoint":
                        # We're ignoring ConditionalPathPoints for now
                        continue

                    elif pp_type == "MovingPathPoint":
                        # We treat this as a fixed PathPoint, definitely not kosher

                        # Get path point location
                        if "location" not in path_point:
                            location = np.array([0, 0, 0], dtype=float)
                        else:
                            location = np.array(path_point["location"].split(), dtype=float)

                        # Transform x,y, and z values (if they are defined) to their mean values to
                        # minimize error
                        location[0] = self.update_moving_path_point_location(
                            "x_location", path_point)
                        location[1] = self.update_moving_path_point_location(
                            "y_location", path_point)
                        location[2] = self.update_moving_path_point_location(
                            "z_location", path_point)

                        # Save the new location and the path point
                        path_point["location"] = Utils.array_to_string(location, abs_thr=EPSILON)
                        self.path_point_set[path_point["body"]].append(path_point)

                        self.sites.append({"@site": path_point["@name"]})

                    else:
                        raise TypeError("Undefined path point type {}".format(pp_type))

            # Finally, we need to sort the sites so that they are in correct order. Unfortunately
            # we have to rely on the site names since xmltodict decomposes the list into
            # dictionaries. There's a pull request in xmltodict for ordering children that might be
            # helpful, but it has not been merged yet

            # Check that the site name prefixes are similar, and only the number is changing
            site_names = [d["@site"] for d in self.sites]
            prefix = os.path.commonprefix(site_names)
            try:
                numbers = [int(name[len(prefix):]) for name in site_names]
            except ValueError:
                raise ValueError("Check these site names, they might not be sorted correctly")

            self.sites = natsorted(self.sites, key=itemgetter(*['@site']), alg=ns.IGNORECASE)

    def update_moving_path_point_location(self, coordinate_name, path_point):
        if coordinate_name in path_point:
            # Parse x and y values
            if "SimmSpline" in path_point[coordinate_name]:
                simmspline = path_point[coordinate_name]["SimmSpline"]
                x_values = np.array(simmspline["x"].split(), dtype=float)
                y_values = np.array(simmspline["y"].split(), dtype=float)
                pp_type = "spline"
            elif "MultiplierFunction" in path_point[coordinate_name]:
                simmspline = path_point[coordinate_name]["MultiplierFunction"]["function"][
                    "SimmSpline"]
                x_values = np.array(simmspline["x"].split(), dtype=float)
                y_values = np.array(simmspline["y"].split(), dtype=float)
                pp_type = "spline"
            elif "NaturalCubicSpline" in path_point[coordinate_name]:
                naturalcubuicspline = path_point[coordinate_name]["NaturalCubicSpline"]
                x_values = np.array(naturalcubuicspline["x"].split(), dtype=float)
                y_values = np.array(naturalcubuicspline["y"].split(), dtype=float)
                pp_type = "spline"
            elif "PiecewiseLinearFunction" in path_point[coordinate_name]:
                pwlf = path_point[coordinate_name]["PiecewiseLinearFunction"]
                x_values = np.array(pwlf["x"].split(), dtype=float)
                y_values = np.array(pwlf["y"].split(), dtype=float)
                pp_type = "piecewise_linear"
            else:
                raise NotImplementedError

            # Fit a cubic spline (if more than 2 values and pp_type is spline), otherwise fit a
            # piecewise linear line
            if len(x_values) > 3 and pp_type == "spline":
                mdl = interp1d(x_values, y_values, kind="cubic", fill_value="extrapolate")
            else:
                mdl = interp1d(x_values, y_values, kind="linear", fill_value="extrapolate")

            # Return the mean of fit inside given range
            x = np.linspace(x_values[0], x_values[-1], 1000)
            return np.mean(mdl(x))

    def get_tendon(self):
        # Return MuJoCo tendon representation of this muscle
        tendon = {"@name": self.name + "_tendon", "site": self.sites}
        if self.tendon_slack_length is not None:
            tendon["@springlength"] = self.tendon_slack_length
        if self.tendon_damping is not None:
            tendon["@damping"] = self.tendon_damping
        return tendon

    def get_actuator(self):
        # Return MuJoCo actuator representation of this actuator
        actuator = {"@name": self.name}
        if self.is_muscle:
            actuator["@tendon"] = self.name + "_tendon"
            actuator["@class"] = "muscle"
            # actuator["@lengthrange"] = Utils.array_to_string(self.length_range)

            # Set timeconst
            if np.all(np.isfinite(self.timeconst)):
                actuator["@timeconst"] = Utils.array_to_string(self.timeconst)
        else:
            # actuator["@gear"] = self.optimal_force
            actuator["@joint"] = self.coordinate
            actuator["@class"] = "motor"

        # Set scale
        # if self.scale is not None:
        #    actuator["@scale"] = str(self.scale)

        # Set ctrl limit
        if np.all(np.isfinite(self.limit)):
            actuator["@ctrllimited"] = "true"
            actuator["@ctrlrange"] = Utils.array_to_string(self.limit)

        return actuator

    def is_disabled(self):
        return self.disabled


def main(argv):
    converter4 = Converter4()
    geometry_folder = None
    for_testing = False
    if len(argv) > 3:
        geometry_folder = argv[3]
    if len(argv) > 4:
        for_testing = True if argv[4].lower() == "true" else False
    converter4.convert(argv[1], argv[2], geometry_folder, for_testing)


if __name__ == "__main__":
    main(sys.argv)
