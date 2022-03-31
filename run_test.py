#!python3.7
import os
import O42MConverter

sik = os.path.join('..', 'stereo_inverse_kinematics')
input_xml = os.path.join(
    sik, 'osim_models', 'osim_model_mojito', 'RightArmAndHand_NoMuscles_Scaled.osim')
geometry_folder = os.path.join(sik, 'osim_models', 'osim_model_mojito', 'Geometry')
output_folder = os.path.join(sik, 'mjc_models', 'mjc_model_mojito')

converter4 = O42MConverter.Converter4()

# custom scene parameters
converter4.model_mujoco_worldbody['geom']['@pos'] = '0 0 -1'

converter4.convert(input_xml, output_folder, geometry_folder, False)
