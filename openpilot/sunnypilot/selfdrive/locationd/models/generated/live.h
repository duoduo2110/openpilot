#pragma once
#include "rednose/helpers/ekf.h"
extern "C" {
void live_update_4(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_9(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_10(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_12(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_35(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_32(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_13(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_14(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_update_33(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void live_H(double *in_vec, double *out_4141618569674559553);
void live_err_fun(double *nom_x, double *delta_x, double *out_336104914132035225);
void live_inv_err_fun(double *nom_x, double *true_x, double *out_7271859698776574756);
void live_H_mod_fun(double *state, double *out_5296745522040587483);
void live_f_fun(double *state, double dt, double *out_6190173082560003234);
void live_F_fun(double *state, double dt, double *out_1775290298854174806);
void live_h_4(double *state, double *unused, double *out_3381985302175387771);
void live_H_4(double *state, double *unused, double *out_7369325968571334034);
void live_h_9(double *state, double *unused, double *out_3366596944174975637);
void live_H_9(double *state, double *unused, double *out_7128136321941743389);
void live_h_10(double *state, double *unused, double *out_5820461675875995871);
void live_H_10(double *state, double *unused, double *out_237044267078725450);
void live_h_12(double *state, double *unused, double *out_6013134882881217907);
void live_H_12(double *state, double *unused, double *out_2349869560539372239);
void live_h_35(double *state, double *unused, double *out_4168878862868689688);
void live_H_35(double *state, double *unused, double *out_395693471785641470);
void live_h_32(double *state, double *unused, double *out_4936970720163542471);
void live_H_32(double *state, double *unused, double *out_3668749623942104845);
void live_h_13(double *state, double *unused, double *out_7665680987382498567);
void live_H_13(double *state, double *unused, double *out_7055416705305814551);
void live_h_14(double *state, double *unused, double *out_3366596944174975637);
void live_H_14(double *state, double *unused, double *out_7128136321941743389);
void live_h_33(double *state, double *unused, double *out_38641774376781073);
void live_H_33(double *state, double *unused, double *out_3546250476424499074);
void live_predict(double *in_x, double *in_P, double *in_Q, double dt);
}