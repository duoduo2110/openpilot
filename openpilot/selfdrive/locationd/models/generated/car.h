#pragma once
#include "rednose/helpers/ekf.h"
extern "C" {
void car_update_25(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_24(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_30(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_26(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_27(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_29(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_28(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_update_31(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void car_err_fun(double *nom_x, double *delta_x, double *out_6607755363165806471);
void car_inv_err_fun(double *nom_x, double *true_x, double *out_8930007875694985889);
void car_H_mod_fun(double *state, double *out_8503116716236050026);
void car_f_fun(double *state, double dt, double *out_1379221821852906246);
void car_F_fun(double *state, double dt, double *out_4439064891851207524);
void car_h_25(double *state, double *unused, double *out_163359513787674561);
void car_H_25(double *state, double *unused, double *out_4980136066246558349);
void car_h_24(double *state, double *unused, double *out_3507524691101779904);
void car_H_24(double *state, double *unused, double *out_2754428282267689787);
void car_h_30(double *state, double *unused, double *out_7028189599067837235);
void car_H_30(double *state, double *unused, double *out_1936554275245058406);
void car_h_26(double *state, double *unused, double *out_2026516331406991590);
void car_H_26(double *state, double *unused, double *out_8721639385120614573);
void car_h_27(double *state, double *unused, double *out_1106869366689336996);
void car_H_27(double *state, double *unused, double *out_238209036555366505);
void car_h_29(double *state, double *unused, double *out_3198557362738331353);
void car_H_29(double *state, double *unused, double *out_2446785619559450590);
void car_h_28(double *state, double *unused, double *out_7505910884103186142);
void car_H_28(double *state, double *unused, double *out_2635613397510079984);
void car_h_31(double *state, double *unused, double *out_8700751911105346625);
void car_H_31(double *state, double *unused, double *out_4949490104369597921);
void car_predict(double *in_x, double *in_P, double *in_Q, double dt);
void car_set_mass(double x);
void car_set_rotational_inertia(double x);
void car_set_center_to_front(double x);
void car_set_center_to_rear(double x);
void car_set_stiffness_front(double x);
void car_set_stiffness_rear(double x);
}